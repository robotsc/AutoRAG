import itertools
import os.path
import pathlib
from typing import Callable, List, Dict

import pandas as pd

from autorag.evaluate.metric import retrieval_token_recall, retrieval_token_precision, retrieval_token_f1
from autorag.strategy import measure_speed, filter_by_threshold, select_best_average
from autorag.utils import validate_qa_dataset, validate_corpus_dataset
from autorag.utils.util import replace_value_in_dict, make_module_file_name, fetch_contents


def run_passage_compressor_node(modules: List[Callable],
                                module_params: List[Dict],
                                previous_result: pd.DataFrame,
                                node_line_dir: str,
                                strategies: Dict,
                                ) -> pd.DataFrame:
    """
    Run evaluation and select the best module among passage compressor modules.

    :param modules: Passage compressor modules to run.
    :param module_params: Passage compressor module parameters.
    :param previous_result: Previous result dataframe.
        Could be retrieval, reranker modules result.
        It means it must contain 'query', 'retrieved_contents', 'retrieved_ids', 'retrieve_scores' columns.
    :param node_line_dir: This node line's directory.
    :param strategies: Strategies for passage compressor node.
        In this node, we use
        You can skip evaluation when you use only one module and a module parameter.
    :return: The best result dataframe with previous result columns.
        This node will replace 'retrieved_contents' to compressed passages, so its length will be one.
    """
    if not os.path.exists(node_line_dir):
        os.makedirs(node_line_dir)
    project_dir = pathlib.PurePath(node_line_dir).parent.parent
    data_dir = os.path.join(project_dir, "data")
    save_dir = os.path.join(node_line_dir, "passage_compressor")
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # run modules
    results, execution_times = zip(*map(lambda task: measure_speed(
        task[0], project_dir=project_dir, previous_result=previous_result, **task[1]), zip(modules, module_params)))
    results = list(results)
    average_times = list(map(lambda x: x / len(results[0]), execution_times))

    # make retrieval contents gt
    qa_data = pd.read_parquet(os.path.join(data_dir, "qa.parquet"))
    corpus_data = pd.read_parquet(os.path.join(data_dir, "corpus.parquet"))
    retrieval_contents_gt = make_contents_gt(qa_data, corpus_data)

    # run metrics before filtering
    if strategies.get('metrics') is None:
        raise ValueError("You must at least one metrics for retrieval contents evaluation."
                         "It can be 'retrieval_token_f1', 'retrieval_token_precision', 'retrieval_token_recall'.")
    results = list(map(lambda x: evaluate_passage_compressor_node(x, retrieval_contents_gt,
                                                                  strategies.get('metrics')), results))

    # save results to folder
    pseudo_module_params = list(map(lambda x: replace_value_in_dict(x[1], 'prompt', str(x[0])),
                                    enumerate(module_params)))
    filepaths = list(map(lambda x: os.path.join(node_line_dir, make_module_file_name(x[0].__name__, x[1])),
                         zip(modules, pseudo_module_params)))
    list(map(lambda x: x[0].to_parquet(x[1], index=False), zip(results, filepaths)))  # execute save to parquet
    filenames = list(map(lambda x: os.path.basename(x), filepaths))

    # make summary file
    summary_df = pd.DataFrame({
        'filename': filenames,
        'module_name': list(map(lambda module: module.__name__, modules)),
        'module_params': module_params,
        'execution_time': average_times,
        **{f'passage_compressor_{metric}': list(map(lambda result: result[metric].mean(), results)) for metric in
           strategies.get('metrics')},
    })

    # filter by strategies
    if strategies.get('speed_threshold') is not None:
        results, filenames = filter_by_threshold(results, average_times, strategies['speed_threshold'], filenames)
    selected_result, selected_filename = select_best_average(results, strategies.get('metrics'), filenames)
    best_result = pd.concat([previous_result, selected_result], axis=1)

    # add summary.parquet 'is_best' column
    summary_df['is_best'] = summary_df['filename'] == selected_filename

    # save the result files
    best_result.to_parquet(os.path.join(save_dir, f'best_{os.path.splitext(selected_filename)[0]}.parquet'),
                           index=False)
    summary_df.to_parquet(os.path.join(save_dir, 'summary.parquet'), index=False)
    return best_result


def evaluate_passage_compressor_node(result_df: pd.DataFrame,
                                     retrieval_contents_gt: List[List[str]],
                                     metrics: List[str]):
    metric_funcs = {
        retrieval_token_recall.__name__: retrieval_token_recall,
        retrieval_token_precision.__name__: retrieval_token_precision,
        retrieval_token_f1.__name__: retrieval_token_f1,
    }
    metrics = list(filter(lambda x: x in metric_funcs.keys(), metrics))
    if len(metrics) <= 0:
        raise ValueError(f"metrics must be one of {metric_funcs.keys()}")
    metrics_scores = dict(map(lambda metric: (metric, metric_funcs[metric](
        gt_contents=retrieval_contents_gt,
        pred_contents=result_df['retrieved_contents'].tolist()
    )), metrics))
    result_df = pd.concat([result_df, pd.DataFrame(metrics_scores)], axis=1)
    return result_df


def make_contents_gt(qa_data: pd.DataFrame, corpus_data: pd.DataFrame) -> List[List[str]]:
    validate_qa_dataset(qa_data)
    validate_corpus_dataset(corpus_data)
    retrieval_gt_ids = qa_data['retrieval_gt'].apply(lambda x: list(itertools.chain.from_iterable(x))).tolist()
    return fetch_contents(corpus_data, retrieval_gt_ids)
