# thoth-lab
# Copyright(C) 2018, 2019, 2020 Marek Cermak, Francesco Murdaca
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Inspection results processing and analysis."""

import functools
import logging
import re
import os

import numpy as np
import pandas as pd

import textwrap
import typing

import cufflinks as cf
import plotly
import plotly.offline as py

from pandas_profiling import ProfileReport as profile
from pandas.io.json import json_normalize

from prettyprinter import pformat

from typing import Any, Dict, List, Tuple, Union
from typing import Callable, Iterable

from pathlib import Path

from plotly import graph_objs as go
from plotly import figure_factory as ff
from plotly import tools

import matplotlib
import matplotlib.pyplot as plt

import seaborn as sns

from thoth.storages import InspectionResultsStore
from thoth.lab.utils import group_index

logger = logging.getLogger("thoth.lab.inspection")

# cufflinks should be in offline mode
cf.go_offline()

sns.set(style="whitegrid")

_INSPECTION_MAPPING_PARAMETERS = {
    "job_duration": "job_duration",
    "build_duration": "build_duration",
    "elapsed": "job_log__stdout__@result__elapsed",
    "rate": "job_log__stdout__@result__rate",
    "utime": "job_log__usage__ru_utime",
    "stime": "job_log__usage__ru_stime",
    "nvcsw": "job_log__usage__ru_nvcsw",
    "nivcsw": "job_log__usage__ru_nivcsw",
}


def extract_structure_json(input_json: dict, upper_key: str, depth: int, json_structure):
    """Convert a json file structure into a list with rows showing tree depths, keys and values.

    :param input_json: inspection result json taken from Ceph
    :param upper_key: key starting point to recursively traverse all tree
    :param depth: depth in the tree
    :param json_structure: recurrent list to store results while traversing the tree
    """
    depth += 1
    for key in input_json.keys():
        if type(input_json[key]) is dict:
            json_structure.append([depth, upper_key, key, [k for k in input_json[key].keys()]])

            extract_structure_json(input_json[key], f"{upper_key}__{key}", depth, json_structure)
        else:
            json_structure.append([depth, upper_key, key, input_json[key]])

    return json_structure


def extract_keys_from_dataframe(df: pd.DataFrame, key: str):
    """Filter the specific dataframe created for a certain key, combination of keys or for a tree depth."""
    if type(key) is str:
        available_keys = set(df["Current_key"].values)
        available_combined_keys = set(df["Upper_keys"].values)

        if key in available_keys:
            ndf = df[df["Current_key"].str.contains(f"^{key}$", regex=True)]

        elif key in available_combined_keys:
            ndf = df[df["Upper_keys"].str.contains(f"{key}$", regex=True)]
        else:
            log.warning("The key is not in the json")
            ndf = "".join(
                [
                    f"The available keys are (WARNING: Some of the keys have no leafs):{available_keys} ",
                    f"The available combined keys are: {available_combined_keys}",
                ]
            )
    elif type(key) is int:
        max_depth = df["Tree_depth"].max()
        if key <= max_depth:
            ndf = df[df["Tree_depth"] == key]
        else:
            ndf = f"The maximum tree depth available is: {max_depth}"
    return ndf


def filter_inspection_ids(inspection_identifiers: List[str]) -> dict:
    """Filter inspection ids list according to the inspection identifier selected.

    :param inspection_identifiers: list of identifier/s to filter inspection ids
    """
    inspection_store = InspectionResultsStore()
    inspection_store.connect()
    filtered_inspection_ids = inspection_store.filter_document_ids(inspection_identifiers=inspection_identifiers)

    inspections_selected = sum([len(batch_n) for batch_n in filtered_inspection_ids.values()])
    inspection_batches = [(batch_name, len(batch_count)) for batch_name, batch_count in filtered_inspection_ids.items()]
    logger.info(f"There are {inspections_selected} inspection runs selected: {inspection_batches} respectively")

    return filtered_inspection_ids


def process_inspection_results(
    inspection_results: List[dict],
    exclude: Union[list, set] = None,
    apply: List[Tuple] = None,
    drop: bool = True,
    verbose: bool = False,
) -> pd.DataFrame:
    """Process inspection result into pd.DataFrame."""
    if not inspection_results:
        return ValueError("Empty iterable provided.")

    datetime_spec = ("created|started_at|finished_at", pd.to_datetime)
    if apply is None:
        apply = [datetime_spec]
    else:
        apply = [*apply, datetime_spec]

    exclude = exclude or []
    apply = apply or ()

    df = json_normalize(inspection_results, sep="__")  # each row resembles InspectionResult

    if len(df) <= 1:
        return df

    for regex, func in apply:
        for col in df.filter(regex=regex).columns:
            df[col] = df[col].apply(func)

    keys = [k for k in inspection_results[0] if k not in exclude]
    for k in keys:
        if k in exclude:
            continue
        d = df.filter(regex=k)
        p = profile(d)

        rejected = (
            p.description_set["variables"]
            .query("distinct_count <= 1 & type != 'UNSUPPORTED'")
            .filter(regex="^((?!version).)*$", axis=0)
        )  # explicitly include versions

        if verbose:
            print("Rejected columns: ", rejected.index)

        if drop:
            df.drop(rejected.index, axis=1, inplace=True)

    df = df.eval(
        "status__job__duration   = status__job__finished_at   - status__job__started_at", engine="python"
    ).eval("status__build__duration = status__build__finished_at - status__build__started_at", engine="python")

    return df


def aggregate_inspection_results_per_identifier(
    inspection_ids: List[str], identifier_inspection: List[str], limit_results: bool = False, max_ids: int = 5
) -> dict:
    """Aggregate inspection results per identifier from inspection documents stored in Ceph.

    :param inspection_ids: list of inspection ids
    :param inspection_identifier: list of identifier/s to filter inspection ids
    :param limit_results: reduce the number of inspection ids used per batch to `max_ids` to test analysis
    :param max_ids: maximum number of inspection ids considered
    """
    inspection_store = InspectionResultsStore()
    inspection_store.connect()

    inspection_results_dict = {}
    tot = sum([len(r) for r in inspection_ids.values()])
    current_identifier_batch_length = 0

    if limit_results:
        logger.info(f"Limiting results to {max_ids} per batch to test functions!!")

    for identifier in identifier_inspection:
        inspection_results_dict[identifier] = []
        logger.info("Analyzing inspection identifer batch: %r", identifier)

        for n, ids in enumerate(inspection_ids[identifier]):
            document = inspection_store.retrieve_document(ids)
            # pop build logs to save some memory (not necessary for now)
            document["build_log"] = None
            logger.info(f"Analysis n.{n + 1 + current_identifier_batch_length}/{tot}")

            inspection_results_dict[identifier].append(document)

            if limit_results:
                if n + 1 == max_ids:
                    break

        current_identifier_batch_length += len(inspection_ids[identifier])

    return inspection_results_dict


def create_duration_dataframe(inspection_df: pd.DataFrame) -> pd.DataFrame:
    """Compute statistics and duration DataFrame."""
    if len(inspection_df) <= 0:
        raise ValueError("Empty DataFrame provided")

    try:
        inspection_df.drop("build_log", axis=1, inplace=True)
    except KeyError:
        pass

    data = (
        inspection_df.filter(like="duration")
        .rename(columns=lambda s: s.replace("status__", "").replace("__", "_"))
        .apply(lambda ts: pd.to_timedelta(ts).dt.total_seconds())
    )

    def compute_duration_stats(group):
        return (
            group.eval("job_duration_mean      = job_duration.mean()", engine="python")
            .eval("job_duration_upper_bound    = job_duration + job_duration.std()", engine="python")
            .eval("job_duration_lower_bound    = job_duration - job_duration.std()", engine="python")
            .eval("build_duration_mean         = build_duration.mean()", engine="python")
            .eval("build_duration_upper_bound  = build_duration + build_duration.std()", engine="python")
            .eval("build_duration_lower_bound  = build_duration - build_duration.std()", engine="python")
        )

    if isinstance(inspection_df.index, pd.MultiIndex):
        n_levels = len(inspection_df.index.levels)

        # compute duration stats for each group separately
        data = data.groupby(level=list(range(n_levels - 1)), sort=False).apply(compute_duration_stats)
    else:
        data = compute_duration_stats(data)

    return data.round(4)


def create_duration_box(data: pd.DataFrame, columns: Union[str, List[str]] = None, **kwargs):
    """Create duration Box plot."""
    columns = columns if columns is not None else data.filter(regex="duration$").columns

    figure = data[columns].iplot(
        kind="box", title=kwargs.pop("title", "InspectionRun duration"), yTitle="duration [s]", asFigure=True
    )

    return figure


def create_duration_scatter(data: pd.DataFrame, columns: Union[str, List[str]] = None, **kwargs):
    """Create duration Scatter plot."""
    columns = columns if columns is not None else data.filter(regex="duration$").columns

    figure = data[columns].iplot(
        kind="scatter",
        title=kwargs.pop("title", "InspectionRun duration"),
        yTitle="duration [s]",
        xTitle="inspection ID",
        asFigure=True,
    )

    return figure


def create_duration_scatter_with_bounds(
    data: pd.DataFrame, col: str, index: Union[list, pd.Index, pd.RangeIndex] = None, **kwargs
):
    """Create duration Scatter plot with upper and lower bounds."""
    df_duration = (
        data[[col]]
        .eval(f"upper_bound = {col} + {col}.std()", engine="python")
        .eval(f"lower_bound = {col} - {col}.std()", engine="python")
    )

    index = index if index is not None else df_duration.index

    if isinstance(index, pd.MultiIndex):
        index = index.levels[-1] if len(index.levels[-1]) == len(data) else np.arange(len(data))

    upper_bound = go.Scatter(
        name="Upper Bound",
        x=index,
        y=df_duration.upper_bound,
        mode="lines",
        marker=dict(color="lightgray"),
        line=dict(width=0),
        fillcolor="rgba(68, 68, 68, 0.3)",
        fill="tonexty",
    )

    trace = go.Scatter(
        name="Duration",
        x=index,
        y=df_duration[col],
        mode="lines",
        line=dict(color="rgb(31, 119, 180)"),
        fillcolor="rgba(68, 68, 68, 0.3)",
        fill="tonexty",
    )

    lower_bound = go.Scatter(
        name="Lower Bound",
        x=index,
        y=df_duration.lower_bound,
        marker=dict(color="lightgray"),
        line=dict(width=0),
        mode="lines",
    )

    data = [lower_bound, trace, upper_bound]
    m = df_duration[col].mean()

    layout = go.Layout(
        yaxis=dict(title="duration [s]"),
        xaxis=dict(title="inspection ID"),
        shapes=[
            {"type": "line", "x0": 0, "x1": len(index), "y0": m, "y1": m, "line": {"color": "red", "dash": "longdash"}}
        ],
        title=kwargs.pop("title", "InspectionRun duration"),
        showlegend=False,
    )

    fig = go.Figure(data=data, layout=layout)

    return fig


def create_duration_histogram(data: pd.DataFrame, columns: Union[str, List[str]] = None, bins: int = None, **kwargs):
    """Create duration Histogram plot."""
    columns = columns if columns is not None else data.filter(regex="duration$").columns

    if not bins:
        bins = np.max([np.lib.histograms._hist_bin_auto(data[col].values, None) for col in columns])

    figure = data[columns].iplot(
        title=kwargs.pop("title", "InspectionRun distribution"),
        yTitle="count",
        xTitle="durations [s]",
        kind="hist",
        bins=int(np.ceil(bins)),
        asFigure=True,
    )

    return figure


def query_inspection_dataframe(inspection_df: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    """Wrapper around _.query method which always include `duration` columns in filter expression."""
    like = kwargs.pop("like", None)
    regex = kwargs.pop("regex", None)

    if like is not None:
        df = inspection_df._.query(*args, like=like, regex=regex, **kwargs)

        if not any(df.columns.str.contains("duration")):
            # duration columns must be present
            df = df.join(inspection_df.filter(like="duration"))

        return df

    elif regex is not None:
        regex += "|(.*duration)"

    return inspection_df._.query(*args, like=like, regex=regex, **kwargs)


def make_subplots(data: pd.DataFrame, columns: List[str] = None, *, kind: str = "box", **kwargs):
    """Make subplots and arrange them in an optimized grid layout."""
    if kind not in ("box", "histogram", "scatter", "scatter_with_bounds"):
        raise ValueError(f"Can NOT handle plot of kind: {kind}.")

    index = data.index.droplevel(-1).unique()

    if len(index.names) > 2:
        logger.warning(f"Can only handle hierarchical index of depth <= 2, got {len(index.names)}. Grouping index.")

        return make_subplots(group_index(data, range(index.nlevels - 1)), columns, kind=kind, **kwargs)

    grid = ff.create_facet_grid(
        data.reset_index(),
        facet_row=index.names[1] if index.nlevels > 1 else None,
        facet_col=index.names[0],
        trace_type="box",  # box does not need data specification
        ggplot2=True,
    )

    shape = np.shape(grid._grid_ref)[:-1]

    sub_plots = tools.make_subplots(
        rows=shape[0],
        cols=shape[1],
        shared_yaxes=kwargs.pop("shared_yaxes", True),
        shared_xaxes=kwargs.pop("shared_xaxes", False),
        print_grid=kwargs.pop("print_grid", False),
    )

    if isinstance(index, pd.MultiIndex):
        index_grid = zip(*index.labels)
    else:
        index_grid = iter(
            np.transpose([np.tile(np.arange(shape[1]), shape[0]), np.repeat(np.arange(shape[0]), shape[1])])
        )

    for idx, grp in data.groupby(level=np.arange(index.nlevels).tolist()):
        if not isinstance(columns, str) and kind == "scatter_with_bounds":
            if columns is None:
                raise ValueError("`scatter_with_bounds` requires `col` argument, not provided.")
            try:
                columns, = columns
            except ValueError:
                raise ValueError("`scatter_with_bounds` does not allow for multiple columns.")

        fig = eval(f"create_duration_{kind}(grp, columns, **kwargs)")

        col, row = map(int, next(index_grid))  # col-first plotting
        for trace in fig.data:
            sub_plots.append_trace(trace, row + 1, col + 1)

    layout = sub_plots.layout
    layout.update(
        title=kwargs.get("title", fig.layout.title),
        shapes=grid.layout.shapes,
        annotations=grid.layout.annotations,
        showlegend=False,
    )

    x_dom_vals = [k for k in layout.to_plotly_json().keys() if "xaxis" in k]
    y_dom_vals = [k for k in layout.to_plotly_json().keys() if "yaxis" in k]

    layout_shapes = pd.DataFrame(layout.to_plotly_json()["shapes"]).sort_values(["x0", "y0"])

    h_shapes = layout_shapes[~layout_shapes.x0.duplicated(keep=False)]
    v_shapes = layout_shapes[~layout_shapes.y0.duplicated(keep=False)]

    # handle single-columns
    h_shapes = h_shapes.query("y1 - y0 != 1")
    v_shapes = v_shapes.query("x1 - x0 != 1")

    # update axis domains and layout
    for idx, x_axis in enumerate(x_dom_vals):
        x0, x1 = h_shapes.iloc[idx % shape[1]][["x0", "x1"]]

        layout[x_axis].domain = (x0 + 0.03, x1 - 0.03)
        layout[x_axis].update(showticklabels=False, zeroline=False)

    for idx, y_axis in enumerate(y_dom_vals):
        y0, y1 = v_shapes.iloc[idx % shape[0]][["y0", "y1"]]

        layout[y_axis].domain = (y0 + 0.03, y1 - 0.03)
        layout[y_axis].update(zeroline=False)

    # correct annotation to match the relevant group and width
    annot_df = pd.DataFrame(layout.to_plotly_json()["annotations"]).sort_values(["x", "y"])
    annot_df = annot_df[annot_df.text.str.len() > 0]

    aw = min(  # annotation width magic
        int(max(60 / shape[1] - (2 * shape[1]), 6)), int(max(30 / shape[0] - (2 * shape[0]), 6))
    )

    for i, annot_idx in enumerate(annot_df.index):
        annot = layout.annotations[annot_idx]

        index_label: Union[str, Any] = annot["text"]
        if isinstance(index, pd.MultiIndex):
            index_axis = i >= shape[1]
            if shape[0] == 1:
                pass  # no worries, the order and label are aight
            elif shape[1] == 1:
                index_label = index.levels[index_axis][max(0, i - 1)]
            else:
                index_label = index.levels[index_axis][i % shape[1]]

        text: str = str(index_label)

        annot["text"] = re.sub(r"^(.{%d}).*(.{%d})$" % (aw, aw), "\g<1>...\g<2>", text)  # Ignore PycodestyleBear (W605)
        annot["hovertext"] = "<br>".join(pformat(index_label).split("\n"))

    # add axis titles as plot annotations
    layout.annotations = (
        *layout.annotations,
        {
            "x": 0.5,
            "y": -0.05,
            "xref": "paper",
            "yref": "paper",
            "text": fig.layout.xaxis["title"]["text"],
            "showarrow": False,
        },
        {
            "x": -0.05,
            "y": 0.5,
            "xref": "paper",
            "yref": "paper",
            "text": fig.layout.yaxis["title"]["text"],
            "textangle": -90,
            "showarrow": False,
        },
    )

    # custom user layout updates
    user_layout = kwargs.pop("layout", None)
    if user_layout:
        layout.update(user_layout)

    return sub_plots


def show_categories(inspection_df: pd.DataFrame):
    """List categories in the given inspection pd.DataFrame."""
    index = inspection_df.index.droplevel(-1).unique()

    results_categories = {}
    for n, idx in enumerate(index.values):
        logger.info(f"\nClass {n + 1}/{len(index)}")

        class_results = {}
        if len(index.names) > 1:
            for name, ind in zip(index.names, idx):
                logger.info(f"{name} : {ind}")
                class_results[name] = ind
        else:
            logger.info(f"{index.names[0]} : {idx}")
            class_results[index.names[0]] = idx
        results_categories[n + 1] = class_results

        frame = inspection_df.loc[idx]
        logger.info(f"Number of rows (jobs) is: {frame.shape[0]}")

    return results_categories


def create_inspection_dataframes(inspection_results_dict: dict) -> dict:
    """Create dictionary with data frame as returned by `process_inspection_results' for each inspection identifier.

    :param inspection_results_dict: dictionary containing inspection results per inspection identifier.
    """
    inspection_df_dict = {}

    for identifier, inspection_results_list in inspection_results_dict.items():
        logger.info(f"Analyzing inspection batch: {identifier}")

        df = process_inspection_results(
            inspection_results_list,
            exclude=["build_log", "created", "inspection_id"],
            apply=[("created|started_at|finished_at", pd.to_datetime)],
            drop=False,
        )

        inspection_df_dict[identifier] = df

        df_duration = create_duration_dataframe(df)
        inspection_df_dict[identifier]["job_duration"] = df_duration["job_duration"]
        inspection_df_dict[identifier]["build_duration"] = df_duration["build_duration"]

    return inspection_df_dict


def create_inspection_analysis_plots(inspection_df: pd.DataFrame):
    """Create inspection analysis plots for the inspection pd.Dataframe.

    :param inspection_df: data frame as returned by `process_inspection_results' for a specific inspection identifier
    """
    # Box plots job duration and build duration
    fig = create_duration_box(inspection_df, ["build_duration", "job_duration"])

    py.iplot(fig)
    # Scatter job duration
    fig = create_duration_scatter(inspection_df, "job_duration", title="InspectionRun job duration")

    py.iplot(fig)
    # Scatter build duration
    fig = create_duration_scatter(inspection_df, "build_duration", title="InspectionRun build duration")

    py.iplot(fig)
    # Histogram
    fig = create_duration_histogram(inspection_df, ["job_duration"])

    py.iplot(fig)


def create_inspection_parameters_dataframes(parameters: List[str], inspection_df_dict: dict) -> Dict[str, pd.DataFrame]:
    """Create pd.DataFrame of selected parameters from inspections results to be used for statistics and error analysis.

    It also outputs batches and parameters map that is necessary for plots.

    :param parameters: inspection parameters used in the analysis
    :param inspection_df_dict: dictionary with data frame as returned by `process_inspection_results' per identifier.
    """
    inspection_parameters_df_dict = {}

    for parameter in parameters:
        parameter_df = pd.DataFrame()

        for identifier in list(inspection_df_dict.keys()):
            additional = pd.DataFrame()
            additional[identifier] = inspection_df_dict[identifier][_INSPECTION_MAPPING_PARAMETERS[parameter]].values
            parameter_df = pd.concat([parameter_df, additional], axis=1)

        inspection_parameters_df_dict[parameter] = parameter_df

    return inspection_parameters_df_dict


def evaluate_statistics(inspection_df: pd.DataFrame, inspection_parameter: str) -> Dict:
    """Evaluate statistical quantities of a specific parameter of inspection results."""
    std_error = inspection_df[inspection_parameter].std() / np.sqrt(inspection_df[inspection_parameter].shape[0])
    std = inspection_df[inspection_parameter].std()
    median = inspection_df[inspection_parameter].median()
    q = inspection_df[inspection_parameter].quantile([0.25, 0.75])
    q1 = q[0.25]
    q3 = q[0.75]
    iqr = q3 - q1
    cv_mean = inspection_df[inspection_parameter].std() / inspection_df[inspection_parameter].mean() * 100
    cv_median = inspection_df[inspection_parameter].std() / inspection_df[inspection_parameter].median() * 100
    cv_q1 = inspection_df[inspection_parameter].std() / q1 * 100
    cv_q3 = inspection_df[inspection_parameter].std() / q3 * 100
    maxr = inspection_df[inspection_parameter].max()
    minr = inspection_df[inspection_parameter].min()

    return {
        "cv_mean": cv_mean,
        "cv_median": cv_median,
        "cv_q1": cv_q1,
        "cv_q3": cv_q3,
        "std_error": std_error,
        "std": std,
        "median": median,
        "q1": q1,
        "q3": q3,
        "iqr": iqr,
        "max": maxr,
        "min": minr,
    }


def evaluate_inspection_statistics(parameters: list, inspection_df_dict: dict) -> dict:
    """Aggregate statistical quantities per inspection parameter for inspection batches.

    :param parameters: inspection parameters used in the analysis
    :param inspection_df_dict: dictionary with data frame as returned by `process_inspection_results' per identifier
    """
    inspection_statistics_dict = {}

    for parameter in parameters:

        evaluated_statistics = {}

        for identifier in list(inspection_df_dict.keys()):
            evaluated_statistics[identifier] = evaluate_statistics(
                inspection_df=inspection_df_dict[identifier],
                inspection_parameter=_INSPECTION_MAPPING_PARAMETERS[parameter],
            )

        aggregated_statistics = {}

        for statistical_quantity in evaluated_statistics[identifier].keys():
            aggregated_statistics[statistical_quantity] = [
                values[statistical_quantity] for values in evaluated_statistics.values()
            ]

        inspection_statistics_dict[parameter] = aggregated_statistics

    return inspection_statistics_dict


def plot_interpolated_statistics_of_inspection_parameters(
    statistical_results_dict: dict,
    identifier_inspection_list: dict,
    inspection_parameters: List[str],
    colour_list: List[str],
    statistical_quantities: List[str],
    title_plot: str = " ",
    title_xlabel: str = " ",
    title_ylabel: str = " ",
    save_result: bool = False,
    project_folder: str = "",
    folder_name: str = "",
):
    """Plot interpolated statistical quantity/ies of inspection parameter/s from different inspection batches."""
    if len(inspection_parameters) == 1 and len(statistical_quantities) > 1:
        if len(colour_list) != len(statistical_quantities):
            logger.warning(f"List of statistical quantities and List of colours shall have the same length!")

        parameter_results = statistical_results_dict[inspection_parameters[0]]

        for i, quantity in enumerate(statistical_quantities):
            plt.plot(identifier_inspection_list, parameter_results[quantity], f"{colour_list[i]}o-", label=quantity)
            i += 1

        plt.title(title_plot)

    elif len(inspection_parameters) > 1 and len(statistical_quantities) == 1:
        if len(inspection_parameters) != len(colour_list):
            logger.warning(f"List of inspection parameters and List of colours shall have the same length!")

        for i, parameter in enumerate(inspection_parameters):
            parameter_results = statistical_results_dict[parameter]
            plt.plot(
                identifier_inspection_list,
                parameter_results[statistical_quantities[0]],
                f"{colour_list[i]}o-",
                label=parameter,
            )
            i += 1

        plt.title(title_plot)

    elif len(inspection_parameters) == 1 and len(statistical_quantities) == 1:
        if len(colour_list) != len(statistical_quantities):
            logger.warning(f"List of statistical quantities and List of colours shall have the same length!")

        parameter_results = statistical_results_dict[inspection_parameters[0]]

        for i, quantity in enumerate(statistical_quantities):
            plt.plot(identifier_inspection_list, parameter_results[quantity], f"{colour_list[i]}o-", label=quantity)
            i += 1

        plt.title(title_plot)

    else:
        logger.warning(
            """Combinations allowed:
                - single inspection parameter | single or multiple statistical quantity/ies
                - single or multiple inspection parameter/s | single statistical quantity
            """
        )

    plt.xlabel(title_xlabel)
    plt.ylabel(title_ylabel)
    plt.tick_params(axis="x", rotation=45)
    plt.legend()

    if save_result:

        if project_folder != "":
            current_path = Path.cwd()
            project_dir_path = current_path.joinpath(project_folder)

            logger.info("Creating Project folder (if not already created!!)")
            os.makedirs(project_dir_path, exist_ok=True)

            if folder_name != "":
                new_dir_path = project_dir_path.joinpath(folder_name)
                logger.info("Creating sub-folder (if not already created!!)")
                os.makedirs(new_dir_path, exist_ok=True)
                plt.savefig(f'{new_dir_path}/{title_plot}_static.png', bbox_inches='tight')

            else:
                plt.savefig(f'{project_dir_path}/{title_plot}_static.png', bbox_inches='tight')
        else:
            logger.warning("No project folder name provided!!")

    plt.show()


def create_inspection_time_dataframe(df_inspection_batches_dict: dict, n_parallel: int = 6) -> pd.DataFrame():
    """Create pd.Dataframe of time of inspections for build and job."""
    tot_time_builds = []
    tot_time_jobs = []
    tot_time_sum_builds_and_jobs = []

    for identifier, dataframe in df_inspection_batches_dict.items():
        tot_time_builds.append(sum(dataframe["build_duration"]) / 3600 / n_parallel)
        tot_time_jobs.append(sum(dataframe["job_duration"]) / 3600 / n_parallel)
        tot_time_sum_builds_and_jobs.append(
            (sum(dataframe["build_duration"]) / 3600 / n_parallel)
            + (sum(dataframe["job_duration"]) / 3600 / n_parallel)
        )

    df_time = pd.DataFrame()
    df_time["batches"] = list(df_inspection_batches_dict.keys())
    df_time["builds_time"] = tot_time_builds
    df_time["jobs_time"] = tot_time_jobs
    df_time["tot_time"] = tot_time_sum_builds_and_jobs

    return df_time


def create_scatter_plots_for_multiple_batches(
    inspection_df_dict: Dict[str, pd.DataFrame],
    list_batches: List[str],
    columns: Union[str, List[str]] = None,
    title_scatter: str = " ",
    x_label: str = " ",
    y_label: str = " ",
):
    """Create Scatter plots for multiple batches.

    :param inspection_df_dict: dictionary with data frame as returned by `process_inspection_results' per identifier
    :param list_batches: list of batches to be used for correlation analysis
    :param columns: parameters to be considered, taken from data frame as returned by `process_inspection_results'
    :param title_scatter: scatter plot name
    :param x_label: x label name
    :param y_label: y label name
    """
    columns = columns if columns is not None else inspection_df_dict[list_batches[0]][columns].columns

    figure = {
        "data": [
            {
                "x": inspection_df_dict[batch][columns[0]],
                "y": inspection_df_dict[batch][columns[1]],
                "name": batch,
                "mode": "markers",
            }
            for batch in list_batches
        ],
        "layout": {"title": title_scatter, "xaxis": {"title": x_label}, "yaxis": {"title": y_label}},
    }

    return figure


# General functions


def create_scatter_and_correlation(
    data: pd.DataFrame, columns: Union[str, List[str]] = None, title_scatter: str = "Scatter plot"
):
    """Create Scatter plot and evaluate correlation coefficients."""
    columns = columns if columns is not None else data[columns].columns

    figure = data[columns].iplot(
        kind="scatter",
        x=columns[0],
        y=columns[1],
        title=title_scatter,
        xTitle=columns[0],
        yTitle=columns[1],
        mode="markers",
        asFigure=True,
    )

    for correlation_type in ["pearson", "spearman", "kendall"]:
        correlation_matrix = data[columns].corr(correlation_type)
        logger.debug(f"\n{correlation_type} correlation results:\n{correlation_matrix}")

    return figure


def create_plot_multiple_batches(
    data: pd.DataFrame,
    plot_type: str = "box" or "hist",
    plot_title: str = " ",
    x_label: str = "",
    y_label: str = "",
    static: str = True,
    save_result: bool = False,
    project_folder: str = "",
    folder_name: str = ""
):
    """Create (Histogram or Box) plot using several columns of the dataframe(static as default)."""
    if not static:

        if plot_type == "box":
            fig = data.iplot(kind="box", theme="white",
                             title=plot_title,
                             xTitle=x_label,
                             yTitle=y_label)

        if plot_type == "hist":
            fig = data.iplot(kind="histogram", theme="white",
                             title=plot_title,
                             xTitle=x_label,
                             yTitle=y_label)

        if save_result:
            logger.warning("Save figure: Not provided for interactive plot yet!!")
        return fig

    if plot_type == "box":
        px = data.plot(kind="box", title=plot_title)
        px.set_xlabel(x_label)
        px.set_ylabel(y_label)
        px.tick_params(axis="x", rotation=45)

    if plot_type == "hist":
        px = data.plot(kind="hist", title=plot_title)
        px.set_xlabel(x_label)
        px.set_ylabel(y_label)
        px.tick_params(axis="x", rotation=45)

    if save_result:

        if project_folder != "":
            current_path = Path.cwd()
            project_dir_path = current_path.joinpath(project_folder)

            logger.info("Creating Project folder (if not already created!!)")
            os.makedirs(project_dir_path, exist_ok=True)

            if folder_name != "":
                new_dir_path = project_dir_path.joinpath(folder_name)
                logger.info("Creating sub-folder (if not already created!!)")
                os.makedirs(new_dir_path, exist_ok=True)
                fig = px.get_figure()
                fig.savefig(f'{new_dir_path}/{plot_title}_static.png', bbox_inches='tight')

            else:
                fig = px.get_figure()
                fig.savefig(f'{project_dir_path}/{plot_title}_static.png', bbox_inches='tight')
        else:
            logger.warning("No project folder name provided!!")

    return px


def create_plot_from_df(
    data: pd.DataFrame,
    columns: Union[str, List[str]] = None,
    title_plot: str = " ",
    x_label: str = " ",
    y_label: str = " ",
    static: str = True,
    save_result: bool = False,
    project_folder: str = "",
    folder_name: str = ""
):
    """Create plot using two columns of the DataFrame."""
    columns = columns if columns is not None else data[columns].columns
    if len(columns) > 2:
        logger.exception("Only two columns can be used!!")

    if not static:

        fig = py.iplot(
            {
                "data": [{"x": data[columns[0]], "y": data[columns[1]], "mode": "lines+markers"}],
                "layout": {"title": title_plot, "xaxis": {"title": x_label}, "yaxis": {"title": y_label}},
            }
        )

        if save_result:
            logger.warning("Save figure: Not provided for interactive plot yet!!")
        return fig

    px = data[columns].plot(x=columns[0], title=title_plot)
    px.set_xlabel(x_label)
    px.set_ylabel(y_label)
    px.tick_params(axis="x", rotation=45)

    if save_result:

        if project_folder != "":
            current_path = Path.cwd()
            project_dir_path = current_path.joinpath(project_folder)

            logger.info("Creating Project folder (if not already created!!)")
            os.makedirs(project_dir_path, exist_ok=True)

            if folder_name != "":
                new_dir_path = project_dir_path.joinpath(folder_name)
                logger.info("Creating sub-folder (if not already created!!)")
                os.makedirs(new_dir_path, exist_ok=True)
                fig = px.get_figure()
                fig.savefig(f'{new_dir_path}/{title_plot}_static.png', bbox_inches='tight')

            else:
                fig = px.get_figure()
                fig.savefig(f'{project_dir_path}/{title_plot}_static.png', bbox_inches='tight')
        else:
            logger.warning("No project folder name provided!!")


def create_violin_plot(
    data: pd.DataFrame,
    plot_title: str = " ",
    x_label: str = "",
    y_label: str = "",
    save_result: bool = False,
    project_folder: str = "",
    folder_name: str = "",
    linewidth: int = 1
):
    """Create violin plot."""
    ax = sns.violinplot(data=data, linewidth=linewidth)
    ax.tick_params(axis="x", rotation=45)
    ax.set(xlabel=x_label, ylabel=y_label, title=plot_title)

    if save_result:

        if project_folder != "":
            current_path = Path.cwd()
            project_dir_path = current_path.joinpath(project_folder)

            logger.info("Creating Project folder (if not already created!!)")
            os.makedirs(project_dir_path, exist_ok=True)

            if folder_name != "":
                new_dir_path = project_dir_path.joinpath(folder_name)
                logger.info("Creating sub-folder (if not already created!!)")
                os.makedirs(new_dir_path, exist_ok=True)
                fig = ax.get_figure()
                fig.savefig(f'{new_dir_path}/{plot_title}_static.png', bbox_inches='tight')

            else:
                fig = ax.get_figure()
                fig.savefig(f'{project_dir_path}/{plot_title}_static.png', bbox_inches='tight')
        else:
            logger.warning("No project folder name provided!!")

def columns_to_analyze(df: pd.DataFrame, low=0, high=len(df_original),
                       display_clusters=False, cluster_by_hue=False) -> pd.DataFrame:
    """Print all columns within dataframe and count of unique column values within limit.
    :param df: data frame to analyze as returned by `process_inspection_results'
    :param low: the lower limit (0 if not specified) of distinct value counts
    :param high: the upper limit (size of data set if not specified) of distinct value counts
    :param display_clusters: if true, displays grouped counts of parameter and parameter sort_values
    :param cluster_by_hue: if true, displays distribution of parameters to analyze sorted by hues
    """
    lst_columns_to_analyze = []

    # Groups every column by unique values
    printmd("#### Columns to analyze, Unique Value Count")
    for i in df:
        try:
            value_count = len(df.groupby(i).count())
            if ((value_count >= low) and (value_count <= high)):
                print(i, value_count)
                lst_columns_to_analyze.append(i)
        except TypeError:
            # Groups every column by unique values if values are in list or dict formats
            try:
                value_count = len(pd.Series(df[i].values).apply(tuple).unique())
                if ((value_count >= low) and (value_count <= high)):
                    print(i, value_count)
                    lst_columns_to_analyze.append(i)
            except TypeError:
                lst_new = (list(df[i].values))
                value_count = len([i for n, i in enumerate(lst_new) if i not in lst_new[n + 1:]])
                if ((value_count >= low) and (value_count <= high)):
                    print(i, value_count)
                    lst_columns_to_analyze.append(i)
                pass

    # Filters data frame to columns with distinct value counts within the limit
    df_analyze = df[lst_columns_to_analyze]
    if display_clusters is True:
        printmd("#### Inspection result count organized by parameter + parameter values")
        try:
            for i in display_jobs_by_subcategories(df_analyze):
                display(i)
        except TypeError:
            pass
    if cluster_by_hue is True:
        if ((low > 0) and (high < 100)):
            printmd("#### Distribution of parameters to analyze organized by hues")
            plot_subcategories_by_hues(df_analyze, df, "status__job__duration")
        else:
            printmd("##### Parameter variance too large to plot by hue/color")

    # In addition to printing, function returns dataframe with results that fall within the limits

    return df_analyze


def display_jobs_by_subcategories(df: pd.DataFrame):
    """Create dataframe with job counts for each subcategory for every column in the data frame.
    :param df: dataframe with columns of unique value counts as returned by columns_to_analyze
    """
    try:
        lst = []
        # Introduce index column for job counts
        df = df.reset_index()
        for i in df.columns:
            created_values = inspection.query_inspection_dataframe(df, groupby=i)
            if not i == 'index':
                df_inspection_id = created_values.groupby([i]).count()
                df_inspection_id = df_inspection_id.filter(['index', i])
                lst.append(df_inspection_id)
        return lst
    except ValueError:
        # Error given if column values in dataframe are constant
        print("Some or all columns passed in are not distinct in values")


def duration_plots(df: pd.DataFrame):
    """Create plots for job and build duration, elapsed time, and lead time.
    :param df: data frame with duration information as returned by process_inspection_results
    """
    fig = plt.figure(figsize=(10, 20))
    ax1 = fig.add_subplot(321)
    ax2 = fig.add_subplot(322)
    ax3 = fig.add_subplot(323)
    ax4 = fig.add_subplot(324)
    ax5 = fig.add_subplot(325)
    ax6 = fig.add_subplot(326)

    df = df.reset_index()

    # Create value lead_time as: created to status__job__started_at
    df["lead_time"] = df["status__job__started_at"].subtract(df["created"]).dt.total_seconds()
    df = df.sort_values(by=["created"])

    # Plot job__duration from inspection results
    s_plot = df.plot.scatter(x='status__job__duration', y='index', c='DarkBlue',
                             title='Job Duration', ax=ax1)
    s_plot.set_xlabel("status__job__duration [ms]")

    # Plot build__duration from inspection results
    s_plot = df.plot.scatter(x='status__build__duration', y='index', c='DarkBlue',
                             title='Build Duration', ax=ax2)
    s_plot.set_xlabel("status__job__duration [ms]")

    # Plot elapsed time from inspection results
    df["job_log__stdout__@result__elapsed"] = df["job_log__stdout__@result__elapsed"]/1000
    s_plot = df.plot.scatter(x='job_log__stdout__@result__elapsed', y='index', c='DarkBlue',
                             title='Elapsed Duration', ax=ax3)
    s_plot.set_xlabel("elapsed [ms]")

    # Plot lag plot of job duration sorted by the start of job
    df = df.sort_values(by=["status__job__started_at"])
    lag_plot(df["status__job__duration"], ax=ax4)
    ax4.set_title("Job Duration Autocorrelation (Sort by job_started)")

    # Plot lag plot of lead time sorted by job created
    df = df.sort_values(by=["created"])
    lag_plot(df["lead_time"], ax=ax5)
    ax5.set_title("lead_duration Autocorrelation (Sort by created)")

    # Plot lag plot of lead time sorted by job started
    df = df.sort_values(by=["status__job__started_at"])
    lag_plot(df["lead_time"], ax=ax6)
    ax6.set_title("lead_duration Autocorrelation (Sort by job_started)")


def plot_subcategories_by_hues(df_cat: pd.DataFrame, df: pd.DataFrame, column):
    """Create scatter plots with parameter categories separated by hues.
    :param df_cat: filtered dataframe with columns to analyze as returned by columns_to_analyze
    :param df: data frame with duration information as returned by process_inspection_results
    :param colum: job duration/build duration columns from 'df'
    """
    df = df.reset_index()
    for i in df_cat:
        g = sns.FacetGrid(df, hue=i, margin_titles=True, height=5, aspect=1)
        g.map(sns.regplot, column, "index", fit_reg=False, x_jitter=.1,  scatter_kws=dict(alpha=0.5))
        g.add_legend()


def concatenated_df(lst_of_df: list, column: string):
    """Reorganize dataframe to show the distribution of jobs in a category across different subsets of data.
    :param lst_of_df: list of inspection result dataframes which can be different datasets or subset of datasets
    :param column: column name or category for grouping to see the distribution of results
    """
    lst_processed = []
    for i in lst_of_df:
        i = i.reset_index()
        df_grouping_category = i.groupby([column]).count()
        col_one = df_grouping_category["index"]
        df_col_one = pd.DataFrame(col_one)
        df_col_one = df_col_one.rename(index=str, columns={"index": "Total jobs:"+"{}".format(len(i))})
        lst_processed.append(df_col_one)

    df_final = pd.concat([i for i in lst_processed], axis=1)
    return df_final


def summary_trace_plot(df: pd.DataFrame, df_categories: pd.DataFrame, lst=[]):
    """Create trace plot scaled by percentage of compositions of each parameter analyzed with clusters to analyze
    separated by hues.
    :param df: data frame with duration information as returned by process_inspection_results
    :param df_categories: filtered dataframe with columns to analyze as returned by columns_to_analyze
    :param lst: dataframes of clustered data (if any) appended to dataframe of entire dataset (ie: [df_left_cluster, df_right_cluster, df_duration])
    """
    fig = plt.figure(figsize=(15, len(df_categories.columns)*4))
    lst_df = []
    for i in df_categories.columns:
        lst_df.append((concatenated_df(lst, i)))
    lst_to_analyze = df_categories.columns
    count = 0
    for i in range(len(df_categories.columns)):
        ax = fig.add_subplot(len(df_categories.columns), 1, i+1)
        lst_df[count].apply(lambda x: x/x.sum()).transpose().plot(kind='bar', stacked=True, ax=ax, sharex=ax)
        ax.legend(title=lst_to_analyze[count], loc=9, bbox_to_anchor=(1.3, 0.7), fontsize='small', fancybox=True)
        count += 1


def summary_bar_plot(df: pd.DataFrame, df_categories: pd.DataFrame, lst_of_clusters):
    """Create trace stacked plot scaled by total jobs of each parameter within clusters (if any).
    :param df: data frame with duration information as returned by process_inspection_results
    :param df_categories:  filtered dataframe with columns to analyze as returned by columns_to_analyze
    :param lst_of_clusters: list of subset dataframes with the last value in list being the entire data set
    """
    fig = plt.figure(figsize=(15, len(df_categories.columns)*7))
    lst_df = []  # list of dataframes, dataframe for each cluster

    for i in df_categories.columns:
        lst_df.append((concatenated_df(lst_of_clusters, i)))

    lst_to_analyze = df_categories.columns
    count = 0

    for i in (lst_df):
        ax = fig.add_subplot(len(df_categories.columns), 1, count+1)
        ax.set_title(lst_to_analyze[count])
        colors = ["#addd8e", "#fdd0a2", "#a6bddb", "#7fcdbb"]

        if (len(lst_of_clusters) > 1):
            lst_cluster = []
            for j in range(len(lst_of_clusters)-1):
                lst_cluster.append('Total jobs:{}'.format(len(lst_of_clusters[j])))
            g = lst_df[count].loc[:, lst_cluster].plot(align='edge', kind="barh", stacked=True,
                                                       color=colors, width=.6, ax=ax)

        g = lst_df[count].loc[:, ['Total jobs:{}'.format(len(lst_of_clusters[-1]))]].plot(align='edge',
                                                                                          kind="barh",
                                                                                          stacked=False,
                                                                                          color="#7fcdbb",
                                                                                          width=.3, ax=ax)
        g.legend(loc=9, bbox_to_anchor=(1.3, 0.7), fontsize='small', fancybox=True)
        x_offset = 1
        y_offset = -.2
        for p in g.patches:
            b = p.get_bbox()
            val = "{0:g}".format(b.x1 - b.x0)
            if (val != '0'):
                g.annotate(val, ((b.x1) + x_offset, b.y1 + y_offset))
        count += 1


def plot_distribution_of_jobs_combined_categories(df_hardware_category: pd.DataFrame, df_duration: pd.DataFrame,
                                                  df_analyze: pd.DataFrame):
    """
    Plots the job duration distribution for each unique hardware combination/configuration of data.
    :param df_hardware_category: dataframe of of parameters to analyze grouped by distinct rows
    :param df_duration:  dataframe with duration information as returned by process_inspection_results
    :param df_analyze: dataframe of parameters that show variation across the clusters
    """
    list_df_combinations = []
    for i in range(len(df_hardware_category)):
        ll = []
        for j in range(len(df_hardware_category.columns)-1):
            ll.append((df_analyze[df_hardware_category.columns[j]] == df_hardware_category.iloc[i, j]))
        df_new = df_duration[np.logical_and.reduce(ll)]

        list_df_combinations.append(df_new)

    fig = plt.figure(figsize=(5, 4*len(df_hardware_category)))
    fig.subplots_adjust(hspace=0.7, wspace=0.7)
    for i in range(len(df_hardware_category)):
        plt.subplot(len(df_hardware_category), 1, i+1)
        g = sns.distplot(list_df_combinations[i]["status__job__duration"], kde=True)
        g.set_title(str(df_hardware_category.loc[i]), fontsize=6)


# Function takes in a column and prints out the feature class
def map_column_to_feature_class(column_name):
    """Helper function that maps a column in the original dataframe to a feature class.
    :param df: data frame returned by process_inspection_results with no columns dropped (drop=False)
    """
    # The keys are keywords to help associate each column with the corresponding feature class.
    software_keys = ["specification__files", "specification__packages", "specification__python__requirements"]
    script_keys = ["job_log__script", "job_log__stderr", "job_log__stdout", "specification__script"]
    hardware_keys = ["hwinfo__cpu", "platform__architecture", "platform__machine", "platform__platform",
                     "platform__processor", "platform__release", "platform__version"]
    if (any(x in column_name for x in hardware_keys)):
        return ("Hardware (ncpus + Platform + Processor)")
    elif(any(x in column_name for x in software_keys)):
        return ("Software stack (files, python packages, python requirements)")
    elif("specification__base" in column_name):
        return ("Base image")
    elif(any(x in column_name for x in script_keys)):
        return ("Script (script + sha256 + parameters)")
    elif("build__requests" in column_name):
        return ("Build request (hardware + memory)")
    elif("run__requests" in column_name):
        return ("Run request (hardware + memory)")
    elif("build__exit_code" in column_name):
        return ("Failure build (exit_code)")
    elif("job__exit_code" in column_name):
        return ("Failure run/job (exit_code)")
    elif("job_log__exit_code" in column_name):
        return ("Job Log (exit_code)")
    else:
        return ("Inspection Result Info")


def process_empty_or_mutable_parameters(df: pd.DataFrame):
    """Print all columns with values of type dictionary/list.
    These values will not work with further processing using the groupby function. Prints the unique
    value count of all columns that are unhashable (all such columns are constant). Drops these
    columns and returns a new dataframe.
    :param df: data frame as returned by process_inspection_results with no columns dropped (drop=False)
    """
    # This is a list to populate with columns with no data or columns with unhashable data.
    list_of_unhashable_none_values = []
    for i in df:
        try:
            value_count = len(df.groupby(i).count())

            # Columns with no data
            if (value_count == 0):
                print(i, value_count)
                list_of_unhashable_none_values.append(i)
        except TypeError:
            # Groups every column by unique values if values are in list or dict formats
            try:
                # If values are type list checks uniqueness
                value_count = len(pd.Series(df[i].values).apply(tuple).unique())
                list_of_unhashable_none_values.append(i)
                print(i, value_count)
            except TypeError:
                # If values are type dict checks uniqueness
                lst_new = (list(df[i].values))
                value_count = len([i for n, i in enumerate(lst_new) if i not in lst_new[n + 1:]])
                list_of_unhashable_none_values.append(i)
                print(i, value_count)
    return df.drop(list_of_unhashable_none_values, axis=1)


# Function takes in dataframe
def unique_value_count_by_feature_class(df: pd.DataFrame):
    """Print unique count values per feature/class.
    Print results per feature/class that are subdivided in subclasses that map to it.
    :param df: processed dataframe as returned by the process_empty_or_mutable_parameters
    """
    dict_to_feature_class = {}
    list_of_features = []

    # Iterate through dataframe and create a dictionary of dataframe column: feature class key value pairs.
    for i in df:
        dict_to_feature_class[i] = map_column_to_feature_class(i)
        list_of_features.append(map_column_to_feature_class(i))

    # Get list of distinct feature class labels (this is generalized to accomodate changes made to the label)
    list_of_features = set(list_of_features)

    # Iterate through every feature class
    for j in list_of_features:
        # Get a list of parameters that fall within the class
        list_of_parameters_per_feature = []
        for k in dict_to_feature_class:
            if (j == dict_to_feature_class[k]):
                list_of_parameters_per_feature.append(k)

        # Groupby to get unique value count for feature class
        try:
            group = df.groupby(list_of_parameters_per_feature).size()
            printmd("#### {} {}".format(j, len(group)))

            # Groupby to get unique value count for each dataframe column
            for l in list_of_parameters_per_feature:
                print(l, len(df.groupby(l).size()))

        except (TypeError, ValueError) as e:
            print("Parameter does not have any values. Filter these out first")


def dataframe_statistics(df: pd.DataFrame, plot_title):
    """Output a data frame with relevant statistics on job duration, build duration and time elapsed.
    :param df: data frame to analyze as returned by `process_inspection_results' with duration values in ms
    :param plot_title: title of fit plot
    """
    # Measure of skew and kurtosis for job and build duration    printmd("## Duration Statistics")
    printmd("#### Skew and kurtosis statistics")
    print("Job Duration Skew:", df["status__job__duration"].skew(axis=0, skipna=True))
    print("Build Duration Skew:", df["status__build__duration"].skew(axis=0, skipna=True))
    print("Job Duration Kurtosis", (df["status__job__duration"].kurt(axis=0, skipna=True)))
    print("Build Duration Kurtosis", (df["status__build__duration"].kurt(axis=0, skipna=True)))

    # Statistics for job duration, build duration, and elapsed time
    printmd("#### Duration statistics")
    df_stats = pd.DataFrame((df["status__job__duration"].describe()))
    df_stats["status__build__duration"] = df["status__build__duration"].describe()
    df_stats["job_log__stdout__@result__elapsed"] = (df["job_log__stdout__@result__elapsed"]/1000).describe()
    display(df_stats)

    # Plotting of job duration and build duration with fit
    g = sns.distplot(df["status__job__duration"], kde=True)
    g.set_title("Job Duration: {}".format(plot_title))
    printmd("#### Job and build distribution plots, scatter plots, autocorrelation plots")
    plt.figure()
    g2 = sns.distplot(df["status__build__duration"], kde=True)
    g2.set_title("Build Duration: {}".format(plot_title))

    plt.figure()

    duration_plots(df_duration)


def printmd(string):
    """Alternate print function implementing markdown formatting.
    :param string: string to print
    """
    display(Markdown(string))
