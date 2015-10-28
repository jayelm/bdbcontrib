# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2014, MIT Probabilistic Computing Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import math
import pickle

import numpy as np
import pandas as pd

from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import Imputer

import bdbcontrib
from bdbcontrib.predictors import predictor
from bdbcontrib.predictors import utils

class MultipleRegression(predictor.IBayesDBForeignPredictor):
    """A linear regression foreign predictor.

    The `targets` must be a single numerical stattype.  The `conditions`
    may be arbitrary numerical or categorical columns.
    """

    @classmethod
    def create(cls, bdb, table, targets, conditions):
        cols = [c for c,_ in targets+conditions]
        df = bdbcontrib.table_to_df(bdb, table, cols)
        mr = cls()
        mr.train(df, targets, conditions)
        mr.prng = bdb.np_prng
        return mr

    @classmethod
    def serialize(cls, _bdb, pred):
        state = {
            'targets': pred.targets,
            'conditions_numerical': pred.conditions_numerical,
            'conditions_categorical': pred.conditions_categorical,
            'mr_full': pred.mr_full,
            'mr_partial': pred.mr_partial,
            'mr_full_noise': pred.mr_full_noise,
            'mr_partial_noise': pred.mr_partial_noise,
            'lookup': pred.lookup
        }
        return pickle.dumps(state)

    @classmethod
    def deserialize(cls, bdb, binary):
        state = pickle.loads(binary)
        mr = cls(targets=state['targets'],
            conditions_numerical=state['conditions_numerical'],
            conditions_categorical=state['conditions_categorical'],
            mr_full=state['mr_full'], mr_partial=state['mr_partial'],
            mr_full_noise=state['mr_full_noise'],
            mr_partial_noise=state['mr_partial_noise'],
            lookup=state['lookup'])
        mr.prng = bdb.np_prng
        return mr

    @classmethod
    def name(cls):
        return 'multiple_regression'

    def __init__(self, targets=None, conditions_numerical=None,
            conditions_categorical=None, mr_full=None, mr_partial=None,
            mr_full_noise=None, mr_partial_noise=None, lookup=None):
        self.targets = targets
        self.conditions_numerical = conditions_numerical
        self.conditions_categorical = conditions_categorical
        if (self.conditions_numerical is not None
                and self.conditions_categorical is not None):
            self.conditions = self.conditions_numerical + \
                self.conditions_categorical
        self.mr_full = mr_full
        self.mr_partial = mr_partial
        self.mr_full_noise = mr_full_noise
        self.mr_partial_noise = mr_partial_noise
        self.lookup = lookup

    def train(self, df, targets, conditions):
        # Obtain the targets column.
        if len(targets) != 1:
            raise ValueError('MultipleRegression requires at least one column '
                'in targets. Received {}'.format(targets))
        if targets[0][1].lower() != 'numerical':
            raise ValueError('MultipleRegression can only regress NUMERICAL '
                'columns. Received {}'.format(targets))
        self.targets = [targets[0][0]]

        # Obtain the condition columns.
        if len(conditions) < 1:
            raise ValueError('MultipleRegression requires at least one '
                'column in conditions. Received {}'.format(conditions))
        self.conditions_categorical = []
        self.conditions_numerical = []
        for c in conditions:
            if c[1].lower() == 'categorical':
                self.conditions_categorical.append(c[0])
            else:
                self.conditions_numerical.append(c[0])
        self.conditions = self.conditions_numerical + \
            self.conditions_categorical

        # The dataset.
        self.dataset = pd.DataFrame()
        # Lookup for categoricals to code.
        self.lookup = dict()
        # Training set (regressors and labels)
        self.X_numerical = np.ndarray(0)
        self.X_categorical = np.ndarray(0)
        self.Y = np.ndarray(0)
        # Linear regressors.
        self.mr_partial = LinearRegression()
        self.mr_full = LinearRegression()

        # Build the foreign predictor.
        self._init_dataset(df)
        self._init_categorical_lookup()
        self._init_X_categorical()
        self._init_X_numerical()
        self._init_Y()
        self._train_mr()

    def _init_dataset(self, df):
        """Create the dataframe of the satellites dataset.

        `NaN` strings are converted to Python `None`.

        Rows where the target is absent are dropped.

        Creates: self.dataset.
        """
        df = df.where((pd.notnull(df)), None)
        self.dataset = df[self.conditions + self.targets].dropna(
            subset=self.targets)

    def _init_categorical_lookup(self):
        """Builds a dictionary of dictionaries. Each dictionary contains the
        mapping category -> code for the corresponding categorical feature.

        Creates: self.lookup
        """
        for categorical in self.conditions_categorical:
            self.lookup[categorical] = {val:code for (code,val) in
                enumerate(self.dataset[categorical].unique())}

    def _init_X_categorical(self):
        """Creates: self.X_categorical.
        """
        self.X_categorical = utils.binarize_categorical_matrix(
            self.conditions_categorical, self.lookup, self.dataset)

    def _init_X_numerical(self):
        """Extract numerical columns from the dataset into a matrix.

        Creates: self.X_numerical
        """
        X_numerical = self.dataset[self.conditions_numerical].as_matrix().\
            astype(float)
        self.X_numerical = Imputer().fit_transform(X_numerical)

    def _init_Y(self):
        """Extracts the targets column.

        Creates: self.Y
        """
        self.Y = self.dataset[self.targets].as_matrix().ravel()

    def _train_mr(self):
        """Trains the regressions.

        We train two regressions, `partial` which is just trained on
        `conditions_numerical`, and `full` which is trained on
        `conditions_numerical+conditions_categorical`.

        This safe-guard feature is critical for querying; otherwise sklearn
        would crash whenever a categorical value unseen in training due to
        filtering (but existant in df nevertheless) was passed in.
        """
        self.mr_partial.fit(self.X_numerical, self.Y)
        self.mr_full.fit(np.hstack((self.X_numerical, self.X_categorical)),
            self.Y)

        self.mr_partial_noise = \
            np.linalg.norm(self.Y-self.mr_partial.predict(
                self.X_numerical))/len(self.Y)

        self.mr_full_noise = \
            np.linalg.norm(self.Y-self.mr_full.predict(
                np.hstack((self.X_numerical, self.X_categorical))))/len(self.Y)

    def _compute_targets_distribution(self, conditions):
        """Given conditions dict {feature_col:val}, returns the conditional
        mean of the `targets`, and the scale of the Gaussian noise.
        """
        if not set(self.conditions).issubset(set(conditions.keys())):
            raise ValueError('Must specify values for all the conditionals.\n'
                'Received: {}\n'
                'Expected: {}'.format(conditions, self.conditions_numerical +
                self.conditions_categorical))

        # Are there any category values in conditions which never appeared during
        # training? If yes, we need to run the partial RF.
        unseen = any([conditions[cat] not in self.lookup[cat]
            for cat in self.conditions_categorical])

        X_numerical = [conditions[col] for col in self.conditions_numerical]

        if unseen:
            inputs = np.array([X_numerical])
            assert inputs.shape == (1, len(self.conditions_numerical))
            predictions = self.mr_partial.predict(inputs)
            noise = self.mr_partial_noise
        else:
            X_categorical = [conditions[col] for col in
                self.conditions_categorical]
            X_categorical = self._binarize_categorical_row(X_categorical)
            inputs = np.concatenate(([X_numerical], [X_categorical]), axis=1)
            assert inputs.shape == \
                (1, len(self.conditions_numerical) + len(X_categorical))
            predictions = self.mr_full.predict(inputs)
            noise = self.mr_full_noise

        return predictions[0], noise

    def simulate(self, n_samples, conditions):
        prediction, noise = self._compute_targets_distribution(conditions)
        return list(prediction + self.prng.normal(scale=noise, size=n_samples))

    def logpdf(self, value, conditions):
        prediction, noise = self._compute_targets_distribution(conditions)
        return logpdfGaussian(value, prediction, noise)

HALF_LOG2PI = 0.5 * math.log(2 * math.pi)
def logpdfGaussian(x, mu, sigma):
    deviation = x - mu
    return - math.log(sigma) - HALF_LOG2PI \
        - (0.5 * deviation * deviation / (sigma * sigma))
