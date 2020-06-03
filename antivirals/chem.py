from __future__ import annotations
from uuid import uuid4
from typing import Sequence, Generator
from multiprocessing import cpu_count
from gensim.models.doc2vec import Doc2Vec, TaggedDocument
from pysmiles.read_smiles import _tokenize
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.feature_selection import mutual_info_classif
from sklearn.metrics import roc_auc_score
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
import numpy as np
from antivirals.schema import Molecules


class _WrapGenerator:
    """
    Creates iterators out of replayable generators. Needed for gensim.
    """

    def __init__(self, func):
        self.func = func
        self.generator = func()

    def __iter__(self):
        self.generator = self.func()
        return self

    def __next__(self):
        res = next(self.generator)
        if not res:
            raise StopIteration
        else:
            return res


class Hyperparameters:
    """
    Hyperparameters for all chemistry models.
    """
    
    # Language hyperparams
    vec_dims = 32
    vec_window = 8
    max_vocab = 18000
    max_ngram = 6
    doc_epochs = 32

    # Toxicity hyperparams
    estimators = 256
    min_samples_split = 6
    min_samples_leaf = 6

    @staticmethod
    def from_dict(values):
        hp = Hyperparameters()
        hp.__dict__.update(values)
        return hp


class Chemistry:
    """
    A master model that encodes all aspects of chemistry.
    """

    toxicity: Toxicity
    language: Language
    hyperparams: Hyperparameters
    uuid: str

    def __init__(
            self,
            hyperparams: Hyperparameters = Hyperparameters()):
        self.hyperparams = hyperparams
        self.language = Language(self.hyperparams)
            self.toxicity = Toxicity(self.hyperparams, self.language)
        self.uuid = str(uuid4())

    def from_molecules(self, mols: Molecules):
        tox_data = mols.get_mols_with_passfail_labels()
        X = tox_data.index
        y = tox_data.astype('int')
        self.language.fit(mols.get_all_mols(), X, y)
        self.toxicity = Toxicity(self.hyperparams, self.language)
        self.toxicity.build(X, y)
        print(f"Trained {self.uuid} -- {self.hyperparams}")

class Toxicity:
    """
    Implments the toxicity model ontop of the latent vectors of the chemical language model.
    """
    auc: Sequence[float]

    def __init__(self, hyperparams: Hyperparameters, language_model: Language):
        self.hyperparams = hyperparams
        self.language = language_model

    def _to_language_vecs(self, X: Sequence[str]) -> np.ndarray:
        # Preallocate memory for performance
        latent_vecs = np.empty(
            (len(X), self.language.document_model.vector_size))

        for i, sent in enumerate(self.language.make_generator(X)):
            latent_vecs[i] = self.language.document_model.infer_vector(sent)

        return latent_vecs

    def fit(self, X: Sequence[str], Y: np.ndarray):
        self.classif = RandomForestClassifier(
            bootstrap=False, criterion='entropy', max_features=0.25,
            min_samples_leaf=self.hyperparams.min_samples_leaf,
            min_samples_split=self.hyperparams.min_samples_split,
            n_estimators=self.hyperparams.estimators)

        self.classif.fit(self._to_language_vecs(X), Y)

    def build(self, X: Sequence[str] = None, Y: np.ndarray = None):
        Xt, Xv, Yt, Yv = train_test_split(X, Y, test_size=0.2, random_state=18)
        self.fit(Xt, Yt)
        self.auc = self.audit(Xv, Yv)

    def audit(self, X: Sequence[str], Y: np.ndarray):
        Yhats = self.classif.predict_proba(self._to_language_vecs(X))
        if 'to_numpy' in dir(Y):
            Y = Y.to_numpy() 
        res = []
        for i, Yhat in enumerate(Yhats):
            res.append(roc_auc_score(Y[:, i], Yhat[:, 1]))     
        return res

class Language:
    """
    A chemical language model that creates semantic latent vectors from chemicals, 
    based on the mutual information between subtokens of a chemical discriptor and 
    a surrigate prediction set encoding chemistry semantics.
    """

    def __init__(self, hyperparams: Hyperparameters):
        self.hyperparams = hyperparams

    def _smiles_to_trivial_lang(self, smiles_seq: Sequence[str]) -> Generator[str, None, None]:
        for smiles in smiles_seq:
            res = []
            for cat, symbol in _tokenize(smiles):
                res.append(cat.name + 'O' + str(symbol))
            yield ' '.join(res)

    def _smiles_to_advanced_lang(self, smiles_seq: Generator
                                 [str, None, None],
                                 training: bool = False) -> Generator[str, None,
                                                                      None]:
        for i, sent in enumerate(smiles_seq):
            sent: Sequence[str] = self._analyzer(sent)
            res: Sequence[str] = []
            for token in sent:
                if token in self.vocab:
                    res.append(token.replace(' ', 'A'))
            if training:
                yield TaggedDocument(words=res, tags=[i])
            else:
                yield res

    def _make_iterator(
            self, smiles_seq: Sequence[str],
            training: bool = False) -> _WrapGenerator:
        return _WrapGenerator(
            lambda: self._smiles_to_advanced_lang(
                self._smiles_to_trivial_lang(smiles_seq),
                training))

    def make_generator(self, X):
        return self._smiles_to_advanced_lang(self._smiles_to_trivial_lang(X))

    def to_vecs(self, smiles_seq: Generator[str, None, None]) -> Generator[np.ndarray, None, None]:
        translation = self.make_generator(smiles_seq)
        for sentence in translation:
            yield self.document_model.infer_vector(sentence)

    def _fit_language(self, X_unmapped: Sequence[str], X: Sequence[str], Y: np.ndarray):
        cv = CountVectorizer(
            max_df=0.95, min_df=2, lowercase=False,
            ngram_range=(1, self.hyperparams.max_ngram),
            max_features=(self.hyperparams.max_vocab * 18),
            token_pattern='[a-zA-Z0-9$&+,:;=?@_/~#\\[\\]|<>.^*()%!-]+')

        X_vec = cv.fit_transform(self._smiles_to_trivial_lang(X))

        local_vocab = set()
        for feat in Y.columns:
            res = zip(cv.get_feature_names(),
                      mutual_info_classif(
                          X_vec, Y[feat], discrete_features=True)
                      )
            local_vocab.update(res)
        self.vocab = {
            i[0]
            for i in sorted(
                local_vocab, key=lambda i: i[1],
                reverse=True)[: self.hyperparams.max_vocab]}

        self._analyzer = cv.build_analyzer()

    def _fit_document_model(self, X_unmapped: Sequence[str], X: Sequence[str], Y: np.ndarray):
        generator = self._make_iterator(X_unmapped, training=True)

        document_model = Doc2Vec(
            vector_size=self.hyperparams.vec_dims, workers=cpu_count(),
            window=self.hyperparams.vec_window)
        document_model.build_vocab(generator)
        document_model.train(
            generator, total_examples=len(X_unmapped), epochs=self.hyperparams.doc_epochs)

        self.document_model = document_model

    def fit(self, X_unmapped: Sequence[str], X: Sequence[str], Y: np.ndarray):
        self._fit_language(X_unmapped, X, Y)
        self._fit_document_model(X_unmapped, X, Y)
