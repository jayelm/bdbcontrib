# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2016, MIT Probabilistic Computing Project
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

# pylint: disable=no-member

import os
import pandas as pd
import re
import sys

import bayeslite
import bayeslite.core
from bayeslite.loggers import BqlLogger, logged_query
from bayeslite.exception import BayesLiteException as BLE

import bdbcontrib
from py_utils import helpsub

class Population(object):
  """Generative Population Model, wraps a BayesDB, and tracks one population."""

  @classmethod
  def method_imports(cls):
    """Runs decorators that add methods to Population."""
    # These are here rather than in, say __init__.py so doing import bdbcontrib
    # just to get its __version__ for example doesn't need to run all that code.
    # __init__.py does import population (this file) for quickstart's __doc__
    # (same as this class's __init__.__doc__) so these can't just be at top.
    # But once you're using Populations, you have to pay this price once.
    import bql_utils
    import plot_utils
    import recipes
    import diagnostic_utils
    import crosscat_utils
    # Convenience alias:
    cls.q = cls.query

  def __init__(self, name, csv_path=None, bdb_path=None, df=None, logger=None,
               session_capture_name=None):
    """Create a Population object, wrapping a bayeslite.BayesDB.

    name : str  REQUIRED.
        A name for the population, should use letters and underscores only.
        This will also be used as a table name in the bdb, and %t in queries
        will expand to this name. %g in queries will expand to the current
        population metamodel, also based on this name.
    csv_path : str
        The path to a comma-separated values file. If specified, will be used
        to populate the bdb. It must exist and be both readable and non-empty.
    df : pandas.DataFrame
        If specified, these data will be used to populate the bdb, superseding
        any csv_path. It must not be empty.
    bdb_path : str
        If specified, store data and analysis results here. If no other data
        source (csv or df) is specified, then it must already have been
        populated. If not specified, we will use a volatile in-memory bdb.
    logger : object
        Something on which we can call .info or .warn to send messages to the
        user. By default a bayeslite.loggers.BqlLogger, but could be QuietLogger
        (only results), SilentLogger (nothing), IpyLogger, CaptureLogger,
        LoggingLogger, or anything else that implements the BqlLogger interface.
    session_capture_name : String
        Signing up with your name and email and sending your session details
        to the MIT Probabilistic Computing Group helps build a community of
        support and helps improve your user experience. You can save your choice
        in a file called 'bayesdb-session-capture-opt.txt' in the directory
        where you run the software, or any parent directory. This option
        overrides any setting in such a file. Any string is interpreted as
        opting in to sending session details. False is interpreted as opting
        out. You must choose. If you choose to use an organization name or
        email, then please send a note to bayesdb@mit.edu to help us connect
        your sessions to you.

        If you encounter a bug, or something surprising, please include your
        session capture name in your report.

        If you opt out, you still allow us to count how often users opt out.

        DO NOT USE THIS SOFTWARE FOR HIPAA-COVERED, PERSONALLY IDENTIFIABLE,
        OR SIMILARLY SENSITIVE DATA! Opting out does not guarantee security.
    """
    Population.method_imports()
    assert re.match(r'\w+', name)
    assert df is not None or csv_path or bdb_path
    self.name = name
    self.generator_name = name + '_cc' # Because we use the default metamodel.
    self.csv_path = csv_path
    self.df = df
    self.bdb_path = bdb_path
    if logger is None:
      if 'IPython' in sys.modules:
        from bdbcontrib.loggers import IPYTHON_LOGGER as ipy
        self.logger = ipy
      else:
        self.logger = BqlLogger()
    else:
      self.logger = logger
    self.bdb = None
    self.status = None
    self.session_capture_name = None
    self.generators = []
    with logged_query('count-beacon', None, name='count-beacon'):
      self.initialize_session_capture(session_capture_name)
    self.initialize()

  def initialize_session_capture(self, name):
    if self.session_capture_name is not None:
      return
    if name is not None:
      self.session_capture_name = name
      return
    # Search for a session-capture name or opt-out saved as a file:
    filename = "bayesdb-session-capture-opt.txt"
    searchdir = os.getcwd()
    while searchdir != os.path.dirname(searchdir):  # While not at root.
      try:
        with open(os.path.join(searchdir, filename), 'r') as optinfile:
          self.session_capture_name = optinfile.read()
          if self.session_capture_name == 'False':
            self.session_capture_name = False
          break
      except IOError:
        pass
      searchdir = os.path.dirname(searchdir)
    # No init option specified, no choice file found. Force the choice.
    if self.session_capture_name is None:
      raise BLE(
        "Please set session_capture_name option to quickstart\n"
        "  to either opt-in or opt-out of sending details of your usage of\n"
        "  this software to the MIT Probabilistic Computing Group.\n\n"
        "If you see this in one of our example notebooks,\n"
        "  return to the starting page, the Index.ipynb, to\n"
        "  make that choice.")

  def initialize(self):
    if self.bdb:
      self.check_representation()
      return
    self.bdb = bayeslite.bayesdb_open(self.bdb_path)
    if not bayeslite.core.bayesdb_has_table(self.bdb, self.name):
      if self.df is not None:
        bayeslite.read_pandas.bayesdb_read_pandas_df(
          self.bdb, self.name, self.df, create=True, ifnotexists=True)
      elif self.csv_path:
        bayeslite.bayesdb_read_csv_file(
          self.bdb, self.name, self.csv_path,
          header=True, create=True, ifnotexists=True)
      else:
        raise BLE(ValueError("No data sources specified, and an empty bdb."))
    self.generators = self.query('''SELECT * FROM bayesdb_generator''')
    if len(self.generators) == 0:
      size = self.query('''SELECT COUNT(*) FROM %t''').ix(0, 0)
      assert 0 < size
      self.query('''
        CREATE GENERATOR %g IF NOT EXISTS FOR %t USING crosscat( GUESS(*) )''')
    self.check_representation()

  def check_representation(self):
    assert self.bdb, "Did you initialize?"
    assert self.session_capture_name is not None

  def interpret_bql(self, query_string):
    '''Replace %t and %g as appropriate.'''
    return re.sub(r'(^|(?<=\s))%t\b',
                  bayeslite.bql_quote_name(self.name),
                  re.sub(r'(^|(?<=\s))%g\b',
                         bayeslite.bql_quote_name(self.generator_name),
                         query_string))

  def analyze(self, models=100, minutes=0, iterations=0, checkpoint=0):
    '''Run analysis.

    models : integer
        The number of models bounds the accuracy of predictive probabilities.
        With ten models, then you get one decimal digit of interpretability,
        with a hundred models, you get two, and so on.
    minutes : integer
        How long you want to let it run.
    iterations : integer
        How many iterations to let it run.

    Returns:
        A report indicating how many models have seen how many iterations,
        and other info about model stability.
    '''
    self.check_representation()
    with logged_query(query_string='recipes.analyze',
                      name=self.session_capture_name):
      if models > 0:
        self.query('INITIALIZE %d MODELS IF NOT EXISTS FOR %s' %
              (models, self.generator_name))
        assert minutes == 0 or iterations == 0
      else:
        models = self.analysis_status().sum()
      if minutes > 0:
        if checkpoint == 0:
          checkpoint = max(1, int(minutes * models / 200))
        analyzer = ('ANALYZE %s FOR %d MINUTES CHECKPOINT %d ITERATION WAIT' %
                    (self.generator_name, minutes, checkpoint))
        with logged_query(query_string=analyzer,
                          name=self.session_capture_name,
                          bindings=self.query('SELECT * FROM %t')):
          self.query(analyzer)
      elif iterations > 0:
        if checkpoint == 0:
          checkpoint = max(1, int(iterations / 20))
        self.query(
            '''ANALYZE %s FOR %d ITERATIONS CHECKPOINT %d ITERATION WAIT''' % (
                self.generator_name, iterations, checkpoint))
      else:
        raise NotImplementedError('No default analysis strategy yet. '
                                  'Please specify minutes or iterations.')
    # itrs = self.per_model_analysis_status()
    # models_with_fewest_iterations =
    #    itrs[itrs['iterations'] == itrs.min('index').head(0)[0]].index.tolist()
    # TODO(gremio): run each model with as many iterations as it needs to get
    # up to where it needs to get to, if that's larger?
    # Nope. Vikash said there's no reason to think that's a good idea. Perhaps
    # even better to have some young models mixed in with the old ones.
    # I still think we should make some recommendation that scales for what
    # "the right thing" is, where that's something that at least isn't known to
    # suck.

    return self.analysis_status()

  def per_model_analysis_status(self):
    """Return the number of iterations for each model."""
    # XXX Move this to bdbcontrib/src/bql_utils.py ?
    self.check_representation()
    try:
      return self.query('''SELECT iterations FROM bayesdb_generator_model
                           WHERE generator_id = (
                            SELECT id FROM bayesdb_generator WHERE name = ?)''',
                        (self.generator_name,))
    except ValueError:
      # Because, e.g. there is no generator yet, for an empty db.
      return None

  def analysis_status(self):
    """Return the count of models for each number of iterations run."""
    self.check_representation()
    itrs = self.per_model_analysis_status()
    if itrs is None or len(itrs) == 0:
      emt = pd.DataFrame(columns=['count of model instances'])
      emt.index.name = 'iterations'
      return emt
    vcs = pd.DataFrame(itrs['iterations'].value_counts())
    vcs.index.name = 'iterations'
    vcs.columns = ['count of model instances']
    self.status = vcs
    return vcs

  def sql_tracing(self, turn_on=True):
    """Trace underlying SQL, for debugging."""
    self.check_representation()
    # Always turn off:
    self.bdb.sql_untrace(self.bdb.sql_tracer)
    if turn_on:
      printer = lambda query, bindings: self.logger.info(
          "query: [%s] bindings: [%s]\n\n", query, bindings)
      self.bdb.sql_trace(printer)

  def reset(self):
    self.check_representation()
    self.query('drop generator if exists %s' % self.generator_name)
    self.query('drop table if exists %s' % self.name)
    self.bdb = None
    self.initialize()

  def specifier_to_df(self, spec):
    if isinstance(spec, pd.DataFrame):
      return spec
    else:
      return self.query(spec)
