#######
#
# Script to reproduce experiments 
#
# Usage:
#
#   1) python experiment.py reproduce 
#       
#   This will reproduce baseline experiments comparing DeepAR with categorical embeddings, 
#   DeepAR with embedding aggregation penalties (L2 + Cosine Distance), and (optionally) 
#   DeepAR with self-supervised reconciliation penalty. See class docstring for description 
#   of keyword arguments.
#
#   2) python experiment.py optimal_rec
#
#   This will reproduce experiments comparing aforementioned DeepAR models to DeepAR models with 
#   MinT reconciliation. See class docstring for description of keyword arguments. First, the reproduce() 
#   method must be called to fit models and produce initial forecasts. Next, the `hts_ols.R` script 
#   must be run in 'R' to generate MinT reconciled forecasts. Finally, the optimal_rec() method 
#   can be run to evaluate and compare unreconciled and reconciled forecasts. 
#
#######

import fire
import pandas as pd
import os

from gluonts.core.component import get_mxnet_context

# First-party imports
from src.data import (
    preprocess_tourism_data, 
    get_bucket_samplers, 
    split, 
    build_datasets 
)
from src.model import (
    fit_deepar, 
    fit_predict_arima, 
    serialize_all,
    unserialize_all
)
from src.evaluation import (
    evaluate_deepar, 
    evaluate_optimal_rec, 
    agg_evaluations, 
    compare_performance
)

class Experiment(object):

    def __init__(
        self,
        datapath: str = 'data/raw/TourismData_v3.csv',
        output_path: str = 'data/preds',
        serialize_path: str = 'data/models',
        reconciled_path: str = 'data/reconciled_preds',
        metrics_file: str = 'data/metrics/baseline.txt',
        horizon: int = 12,
        train_size: int = 108,
        epochs: int = 50,
        val_set: bool = False,
        include_self_supervised: bool = False,
        include_arima: bool = False,
        embed_dim_ratio: int = 2,
        embed_penalty_lambda: int = 1,
        self_sup_penalty_lambda: float = 10e-7,
    ) -> None:

        """ convenience class to reproduce experiments
        
        Keyword Arguments:
            datapath {str} -- Australia tourism datapath: (default: {'data/raw/TourismData_v3.csv'})
            output_path {str} -- path to write predictions for post-hoc reconciliation 
                (default: {'data/preds'})
            serialize_path {str} -- path to write serialized models for comparison after post-hoc reconciliation 
                (default: {'data/models'})
            reconciled_path {str} -- path to read reconciled predictions from after post-hoc reconciliation (R script)
                (default: {'data/reconciled_preds'})
            metrics_file {str} -- path to which to write performance comparison metrics 
                (default: {'data/metrics/baseline.txt'})
            horizon {int} -- prediction length (default: {12})
            train_size {int} -- number of timesteps in training set (default: {108})
            epochs {int} -- number of training epochs (default: {10})
            val_set {bool} -- whether to include a validation set while training (default: {True})
            include_self_supervised {bool} -- whether to include a model with self-supervised reconciliation
                penalty (default: {True})
            include_arima {bool} -- whether to include a baseline auto arima model, independently fit on every series
                in the dataset (default: {True})
            embed_dim_ratio {int} -- ratio between embedding dim and RNN hidden state dim (default: {1})
            embed_penalty_lambda {int} -- lambda for embedding reconciliation penalty (default: {1})
            self_sup_penalty_lambda {float} -- lambda for self-supervised reconciliation penalty 
                (default: {10e-7})
        
        Raises:
            ValueError: The reproduce() method must be called before the optimal_rec() method
                to generate predictions that can be post-hoc reconciled
        
        """

        self.datapath = datapath
        self.horizon = horizon
        self.train_size = train_size
        self.epochs = epochs
        self.val_set = val_set
        self.include_self_supervised = include_self_supervised
        self.include_arima = include_arima
        self.embed_dim_ratio = embed_dim_ratio
        self.embed_penalty_lambda = embed_penalty_lambda
        self.self_sup_penalty_lambda = self_sup_penalty_lambda
        self.output_path = output_path
        self.serialize_path = serialize_path
        self.reconciled_path = reconciled_path
        self.metrics_file = metrics_file
        self.fits = None
        self.model_names = [
            'DeepAR-Cat-Var',
            'DeepAR-Embed-Agg-Cosine',
            'DeepAR-Embed-Agg-L2',
            'DeepAR-Self-Sup',
            'Arima-Baseline',
            'Arima-Baseline-MinT',
            'DeepAR-Cat-Var-MinT',
            'DeepAR-Embed-Agg-Cosine-MinT',
            'DeepAR-Embed-Agg-L2-MinT'
        ]
        if not self.include_self_supervised:
            self.model_names.remove('DeepAR-Self-Sup')
            self.active_names = self.model_names[:3]
        else:
            self.active_names = self.model_names[:4]

        # mkdirs for output paths if they dont exist
        if not os.path.isdir(output_path):
            os.mkdir(output_path)
        if not os.path.isdir(serialize_path):
            os.mkdir(serialize_path)
        if not os.path.isdir(reconciled_path):
            os.mkdir(reconciled_path)
        metric_dir = os.path.sep.join(metrics_file.split(os.path.sep)[:-1])
        if not os.path.isdir(metric_dir):
            os.mkdir(metric_dir)
        self.data_dir = os.path.sep.join(output_path.split(os.path.sep)[:-1])
        
        print(f'Using device: {get_mxnet_context()}')

    def preprocess(self) -> None:
        
        ## prepare data, create mappings of hierarchy that will be used for fitting/evaluation
        data, self.hierarchy_agg_dict, self.hierarchy_level_dict = preprocess_tourism_data(self.datapath)

        # create train/val/test datasets, one for each of 10 CV folds
        splits = split(
            data.values, 
            horizon = self.horizon, 
            min_train_size = self.train_size, 
            max_train_size = self.train_size
        )

        self.test_datasets = build_datasets(data, splits, val = False)
        if self.val_set:
            self.train_datasets = build_datasets(data, splits)
        else:
            self.train_datasets = [
                (train, None, None)
                for (train, test) in self.test_datasets
            ]
        self.samplers = get_bucket_samplers([data_splits[0] for data_splits in self.train_datasets])


    def fit(self):

        self.fits = [
            # baseline DeepAR model with the learned categorical embedding
            [
                fit_deepar(
                    training_data, 
                    validation_data,
                    pred_length=self.horizon,
                    epochs=self.epochs,
                    use_cat_var=True,
                    cardinality=[len(training_data)],
                    sampler=sampler,
                    hierarchy_agg_dict=self.hierarchy_agg_dict,
                    embedding_dim_ratio=self.embed_dim_ratio,
                    print_rec_penalty=False,
                )
                for (training_data, validation_data, _), sampler in zip(
                    self.train_datasets, 
                    self.samplers
                )
            ],
            # DeepAR models with cosine embedding aggregation penalty
            [
                fit_deepar(
                    training_data, 
                    validation_data,
                    pred_length=self.horizon,
                    epochs=self.epochs,
                    use_cat_var=True,
                    cardinality=[len(training_data)],
                    sampler=sampler,
                    hierarchy_agg_dict=self.hierarchy_agg_dict,
                    embedding_dim_ratio=self.embed_dim_ratio,
                    embedding_agg_penalty=self.embed_penalty_lambda,
                    print_rec_penalty=False,
                )
                for (training_data, validation_data, _), sampler in zip(
                    self.train_datasets, 
                    self.samplers
                )            ],
            # DeepAR models with l2 embedding aggregation penalty
            [
                fit_deepar(
                    training_data, 
                    validation_data,
                    pred_length=self.horizon,
                    epochs=self.epochs,
                    use_cat_var=True,
                    cardinality=[len(training_data)],
                    sampler=sampler,
                    hierarchy_agg_dict=self.hierarchy_agg_dict,
                    embedding_dim_ratio=self.embed_dim_ratio,
                    embedding_agg_penalty=self.embed_penalty_lambda,
                    embedding_dist_metric='l2',
                    print_rec_penalty=False,
                )
                for (training_data, validation_data, _), sampler in zip(
                    self.train_datasets, 
                    self.samplers
                )            ]
        ]
        if self.include_self_supervised:
            self.fits.append(
                [
                    fit_deepar(
                        training_data, 
                        validation_data,
                        pred_length=self.horizon,
                        epochs=self.epochs,
                        use_cat_var=True,
                        cardinality=[len(training_data)],
                        hierarchy_agg_dict=self.hierarchy_agg_dict,
                        embedding_dim_ratio=self.embed_dim_ratio,
                        self_supervised_penalty=self.self_sup_penalty_lambda,
                        print_rec_penalty=False
                    )
                    for (training_data, validation_data, _) in self.train_datasets
                ] 
            )
        if self.include_arima:
            filenames = [
                f'{self.output_path}/{self.model_names[-4]}-fold-{i}.csv' 
                for i in range(len(self.train_datasets))
            ]
            [
                fit_predict_arima(
                    training_data, 
                    horizon=self.horizon, 
                    output_file=filename
                )
                for (training_data, _), filename in zip(self.test_datasets, filenames)
            ]
            

    def serialize(self) -> None:

        [
            serialize_all(fit, f'{self.serialize_path}/{name}') 
            for fit, name in zip(self.fits, self.active_names)
        ]

    def unserialize(self) -> None:

        self.fits = [unserialize_all(f'{self.serialize_path}/{name}') for name in self.active_names]

    def evaluate(self) -> None:
        
        all_filenames = [
            [f'{self.output_path}/{name}-fold-{i}.csv' for i in range(len(self.train_datasets))]
            for name in self.model_names[:3]
        ]
        self.evaluations = [
            [
                evaluate_deepar(
                    predictor, 
                    train_data, 
                    test_data, 
                    self.hierarchy_level_dict,
                    filename
                ) 
                for predictor, (train_data, test_data), filename in zip(models, self.test_datasets, filenames)
            ]
            for models, filenames in zip(self.fits[:3], all_filenames)
        ]
        if self.include_self_supervised:
            self.evaluations.append(
                [
                    evaluate_deepar(
                        predictor, 
                        train_data, 
                        test_data, 
                        self.hierarchy_level_dict
                    ) 
                    for predictor, (train_data, test_data) in zip(self.fits[3], self.test_datasets)
                ]
            )
        if self.include_arima:
            arima_preds = [
                pd.read_csv(f'{self.output_path}/{self.model_names[-4]}-fold-{i}.csv').tail(self.horizon)
                for i in range(len(self.train_datasets))
            ]
            self.evaluations.append(
                [
                    evaluate_optimal_rec(preds, test_data, self.hierarchy_level_dict) 
                    for preds, (_, test_data) in zip(arima_preds, self.test_datasets)
                ]
            )
            self.active_names += [self.model_names[-4]]

    def evaluate_reconciled(self) -> None:

        if self.include_arima:
            names = self.model_names[-4:]
        else:
            names = self.model_names[-3:]
        self.active_names += names

        reconciled_preds = [
            [
                pd.read_csv(f'{self.reconciled_path}/{name[:-5]}-fold-{i}-reconciled.csv') for i in range(len(self.train_datasets))
            ]
            for name in names
        ]

        reconciled = [
            [
                evaluate_optimal_rec(preds, test_data, self.hierarchy_level_dict) 
                for preds, (_, test_data) in zip(preds_list, self.test_datasets)
            ]
            for preds_list in reconciled_preds
        ]

        self.evaluations = self.evaluations + reconciled

    def compare(self) -> None:

        agg_evals = [agg_evaluations(evaluation) for evaluation in self.evaluations]
        compare_performance(
            agg_evals,
            model_names=self.active_names,
            levels=['all', 'country', 'region-by-travel'],
            outfile=self.metrics_file
        )

    def reproduce(self) -> None:

        self.preprocess()
        self.fit()
        self.serialize()
        self.unserialize()
        self.evaluate()
        self.compare()

    def optimal_reconciliation(self) -> None:

        self.preprocess()
        self.unserialize()
        self.evaluate()
        self.evaluate_reconciled()
        self.compare()
        
if __name__ == '__main__':
    fire.Fire(Experiment)