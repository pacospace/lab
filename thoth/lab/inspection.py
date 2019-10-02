# thoth-lab
# Copyright(C) 2018, 2019 Marek Cermak, Francesco Murdaca
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

from plotly import graph_objs as go
from plotly import figure_factory as ff
from plotly import tools

import matplotlib
import matplotlib.pyplot as plt

from thoth.storages import InspectionResultsStore
from thoth.lab.utils import group_index

logger = logging.getLogger("thoth.lab.inspection")

# cufflinks should be in offline mode
cf.go_offline()


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


def filter_inspection_ids_list(inspection_identifier_list: List[str]) -> dict:
    """Filter inspection ids list according to the inspection identifier selected.

    :param inspection_identifier_list: list of identifier to filter out inspection ids
    """
    inspection_store = InspectionResultsStore()
    inspection_store.connect()
    logger.info(f"Retrieving all inspection ids")
    inspection_ids_list = list(inspection_store.get_document_listing())

    filtered_list_ids = {}

    for identifier in inspection_identifier_list:
        filtered_list_ids[identifier] = []

    for ids in inspection_ids_list:
        inspection_filter = "-".join(ids.split("-")[1:(len(ids.split("-")) - 1)])

        if inspection_filter:
            if inspection_filter in inspection_identifier_list:
                filtered_list_ids[inspection_filter].append(ids)

    tot_inspections_selected = sum([len(batch_n) for batch_n in filtered_list_ids.values()])
    inspection_batches = [(batch_name, len(batch_count)) for batch_name, batch_count in filtered_list_ids.items()]
    logger.info(f"There are {tot_inspections_selected} inspection runs selected: {inspection_batches} respectively")

    return filtered_list_ids


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


def aggregate_inspection_results_dict(
    list_ids: List[str], identifier_inspection: List[str], limit_results: bool = False
) -> dict:
    """Aggregate inspection results per identifier from inspection documents stored in Ceph."""
    inspection_store = InspectionResultsStore()
    inspection_store.connect()

    inspection_results_dict = {}
    tot = sum([len(r) for r in list_ids.values()])
    current_identifier_batch_length = 0

    if limit_results:
        logger.info(f"Limiting results to 5 per batch to test functions!!")

    for identifier in identifier_inspection:
        inspection_results_dict[identifier] = []
        logger.info("Analyzing inspection identifer batch: %r", identifier)
        for n, ids in enumerate(list_ids[identifier]):
            document = inspection_store.retrieve_document(ids)
            # pop build logs to save some memory (not necessary for now)
            document["build_log"] = None
            logger.info(f"Analysis n.{n + 1 + current_identifier_batch_length}/{tot}")
            inspection_results_dict[identifier].append(document)
            if limit_results:
                if n + 1 == 5:
                    break

        current_identifier_batch_length += len(list_ids[identifier])

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
        logger.debug(f"\nClass {n + 1}/{len(index)}")

        class_results = {}
        if len(index.names) > 1:
            for name, ind in zip(index.names, idx):
                logger.debug(f"{name} : {ind}")
                class_results[name] = ind
        else:
            logger.debug(f"{index.names[0]} : {idx}")
            class_results[index.names[0]] = idx
        results_categories[n + 1] = class_results

        frame = inspection_df.loc[idx]
        logger.debug(f"Number of rows (jobs) is: {frame.shape[0]}")

    return results_categories


def create_inspection_results_df_dict(inspection_results_dict: dict) -> dict:
    """Create dictionary with pd.Dataframe of inspection results for each inspection identifier.

    :param inspection_results: dictionary containing inspection results retrieved from Ceph.
    """
    inspection_results_df_dict = {}

    for identifier, inspection_results_list in inspection_results_dict.items():
        logger.info(f"Analyzing inspection batch: {identifier}")

        df = process_inspection_results(
            inspection_results_list,
            exclude=["build_log", "created", "inspection_id"],
            apply=[("created|started_at|finished_at", pd.to_datetime)],
            drop=False,
        )

        inspection_results_df_dict[identifier] = df

        df_duration = create_duration_dataframe(df)
        inspection_results_df_dict[identifier]["job_duration"] = df_duration["job_duration"]
        inspection_results_df_dict[identifier]["build_duration"] = df_duration["build_duration"]

    return inspection_results_df_dict


def create_inspection_analysis_plots(df_inspection: pd.DataFrame):
    """Create inspection analysis plots for the inspection pd.Dataframe.

    :param df_inspection: inspection results pd.DataFrame for a specific inspection identifier
    """
    # Box plots job duration and build duration
    fig = create_duration_box(df_inspection, ["build_duration", "job_duration"])

    py.iplot(fig)
    # Scatter job duration
    fig = create_duration_scatter(df_inspection, "job_duration", title="InspectionRun job duration")

    py.iplot(fig)
    # Scatter build duration
    fig = create_duration_scatter(df_inspection, "build_duration", title="InspectionRun build duration")

    py.iplot(fig)
    # Histogram
    fig = create_duration_histogram(df_inspection, ["job_duration"])

    py.iplot(fig)


def create_inspection_batches_parameters_dataframe(
    parameters_map: dict, inspection_results_batches_dict: dict, identifier_list: List[str]
) -> Tuple[pd.DataFrame, Dict]:
    """The function creates pd.DataFrame of selected parameters to be used for statistics and error analysis.

    It also outputs batches and parameters mapping that is necessary for plots.
    """
    df_parameters = pd.DataFrame()
    batches_parameter_map = {}
    for key, parameter in parameters_map.items():
        batches_parameter_map[parameter] = []
        for identifier in identifier_list:
            df_parameters[parameter + "_" + str(identifier.split("-")[0])] = inspection_results_batches_dict[
                identifier
            ][key]
            batches_parameter_map[parameter].append(parameter + "_" + str(identifier.split("-")[0]))

    return df_parameters, batches_parameter_map


def evaluate_statistics(df_inspection: pd.DataFrame, inspection_parameter: str) -> Dict:
    """Evaluate statistical quantities of a specific parameter of inspection results."""
    cv = df_inspection[inspection_parameter].std() / df_inspection[inspection_parameter].mean() * 100
    std_error = df_inspection[inspection_parameter].std() / np.sqrt(df_inspection[inspection_parameter].shape[0])
    std = df_inspection[inspection_parameter].std()
    median = df_inspection[inspection_parameter].median()
    q = df_inspection[inspection_parameter].quantile([0.25, 0.75])
    q1 = q[0.25]
    q3 = q[0.75]
    iqr = q3 - q1
    maxr = df_inspection[inspection_parameter].max()
    minr = df_inspection[inspection_parameter].min()

    return {
        "cv": cv,
        "std_error": std_error,
        "std": std,
        "median": median,
        "q1": q1,
        "q3": q3,
        "iqr": iqr,
        "max": maxr,
        "min": minr,
    }


def evaluate_inspection_statistics_result_dict(
    df_inspection_batches_dict: dict, list_inspection_identifiers: List[str], inspection_parameter: str
) -> dict:
    """Aggregate statistical quantities per inspection parameter for inspection batches."""
    evaluated_statistics = {}
    for identifier in list_inspection_identifiers:
        evaluated_statistics[identifier] = evaluate_statistics(
            df_inspection=df_inspection_batches_dict[identifier], inspection_parameter=inspection_parameter
        )

    aggregated_statistics = {}

    for statistical_quantity in evaluated_statistics[identifier].keys():
        aggregated_statistics[statistical_quantity] = [
            values[statistical_quantity] for values in evaluated_statistics.values()
        ]

    return aggregated_statistics


def plot_interpolated_statistics_of_inspection_parameters(
    statistical_results_dict: dict,
    identifier_inspection_list: dict,
    inspection_parameters: List[str],
    colour_list: List[str],
    statistical_quantities: List[str],
    title_ylabel: str = " ",
):
    """Plot interpolated statistical quantity/ies of inspection parameter/s from different inspection batches."""
    if len(inspection_parameters) == 1 and len(statistical_quantities) >= 1:
        if len(colour_list) != len(statistical_quantities):
            logger.warning(f"List of statistical quantities and List of colours shall have the same length!")
        parameter_results = statistical_results_dict[inspection_parameters[0]]
        for i, quantity in enumerate(statistical_quantities):
            plt.plot(identifier_inspection_list, parameter_results[quantity], f"{colour_list[i]}o-", label=quantity)
            i += 1
        plt.title(f"Statistics plot for {inspection_parameters} of different batch")

    elif len(inspection_parameters) >= 1 and len(statistical_quantities) == 1:
        if len(inspection_parameters) != len(colour_list):
            logger.warning(f"List of inspection parameters and List of colours shall have the same length!")
        for i, parameter in enumerate(inspection_parameters):
            parameter_results = dftotal_statistics[parameter]
            plt.plot(
                identifier_inspection_list,
                parameter_results[statistical_quantities[0]],
                f"{colour_list[i]}o-",
                label=parameter,
            )
            i += 1
        plt.title(f"Statistics plot for {statistical_quantities} of different batch for different parameters")
    else:
        logger.warning(
            """Combinations allowed:
                - single inspection parameter | single or multiple statistical quantity/ies
                - single or multiple inspection parameter/s | single statistical quantity
            """
        )

    plt.xlabel("Batch Identifier")
    plt.ylabel(title_ylabel)
    plt.tick_params(axis="x", rotation=45)
    plt.legend()
    plt.show()


def create_inspections_time_dataframe(
    df_inspection_batches_dict: dict, inspection_identifiers: List[str], n_parallel: int = 6
) -> pd.DataFrame():
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
    df_time["batches"] = inspection_identifiers
    df_time["builds_time"] = tot_time_builds
    df_time["jobs_time"] = tot_time_jobs
    df_time["tot_time"] = tot_time_sum_builds_and_jobs

    return df_time


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


def create_box_plot(
    data: pd.DataFrame,
    columns: Union[str, List[str]] = None,
    title_box: str = "Box plot",
    x_label: str = "",
    y_label: str = "",
    static: str = True,
):
    """Create duration Box plot (static as default)."""
    columns = columns if columns is not None else data[columns].columns
    if not static:
        fig = data[columns].iplot(kind="box", title=title_box, yTitle=y_label, asFigure=True)

        return fig

    ax = data[columns].plot(kind="box", title=title_box)
    ax.set_ylabel(x_label)
    ax.set_ylabel(y_label)


def create_plot_from_df(
    data: pd.DataFrame,
    columns: Union[str, List[str]] = None,
    title_plot: str = " ",
    x_label: str = " ",
    y_label: str = " ",
    static: str = True,
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

        return fig

    px = data[columns].plot(title=title_plot)
    x_label = px.set_xlabel(x_label)
    y_label = px.set_ylabel(y_label)
