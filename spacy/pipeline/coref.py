from typing import Iterable, Tuple, Optional, Dict, Callable, Any, List
import warnings

from thinc.types import Floats2d, Floats3d, Ints2d
from thinc.api import Model, Config, Optimizer, CategoricalCrossentropy
from thinc.api import set_dropout_rate, to_categorical
from itertools import islice
from statistics import mean

from .trainable_pipe import TrainablePipe
from ..language import Language
from ..training import Example, validate_examples, validate_get_examples
from ..errors import Errors
from ..scorer import Scorer
from ..tokens import Doc
from ..vocab import Vocab

from ..ml.models.coref_util import (
    create_gold_scores,
    MentionClusters,
    create_head_span_idxs,
    get_clusters_from_doc,
    get_predicted_clusters,
    DEFAULT_CLUSTER_PREFIX,
    doc2clusters,
)

from ..coref_scorer import Evaluator, get_cluster_info, b_cubed, muc, ceafe


default_config = """
[model]
@architectures = "spacy.Coref.v1"
embedding_size = 20
hidden_size = 1024
n_hidden_layers = 1
dropout = 0.3
rough_k = 50
a_scoring_batch_size = 512
sp_embedding_size = 64

[model.tok2vec]
@architectures = "spacy.Tok2Vec.v2"

[model.tok2vec.embed]
@architectures = "spacy.MultiHashEmbed.v1"
width = 64
rows = [2000, 2000, 1000, 1000, 1000, 1000]
attrs = ["ORTH", "LOWER", "PREFIX", "SUFFIX", "SHAPE", "ID"]
include_static_vectors = false

[model.tok2vec.encode]
@architectures = "spacy.MaxoutWindowEncoder.v2"
width = ${model.tok2vec.embed.width}
window_size = 1
maxout_pieces = 3
depth = 2
"""
DEFAULT_MODEL = Config().from_str(default_config)["model"]

DEFAULT_CLUSTERS_PREFIX = "coref_clusters"


@Language.factory(
    "coref",
    assigns=["doc.spans"],
    requires=["doc.spans"],
    default_config={
        "model": DEFAULT_MODEL,
        "span_cluster_prefix": DEFAULT_CLUSTER_PREFIX,
    },
    default_score_weights={"coref_f": 1.0, "coref_p": None, "coref_r": None},
)
def make_coref(
    nlp: Language,
    name: str,
    model,
    span_cluster_prefix: str = "coref",
) -> "CoreferenceResolver":
    """Create a CoreferenceResolver component."""

    return CoreferenceResolver(
        nlp.vocab, model, name, span_cluster_prefix=span_cluster_prefix
    )



class CoreferenceResolver(TrainablePipe):
    """Pipeline component for coreference resolution.

    DOCS: https://spacy.io/api/coref (TODO)
    """

    def __init__(
        self,
        vocab: Vocab,
        model: Model,
        name: str = "coref",
        *,
        span_mentions: str = "coref_mentions",
        span_cluster_prefix: str,
    ) -> None:
        """Initialize a coreference resolution component.

        vocab (Vocab): The shared vocabulary.
        model (thinc.api.Model): The Thinc Model powering the pipeline component.
        name (str): The component instance name, used to add entries to the
            losses during training.
        span_mentions (str): Key in doc.spans where the candidate coref mentions
            are stored in.
        span_cluster_prefix (str): Prefix for the key in doc.spans to store the
            coref clusters in.

        DOCS: https://spacy.io/api/coref#init (TODO)
        """
        self.vocab = vocab
        self.model = model
        self.name = name
        self.span_mentions = span_mentions
        self.span_cluster_prefix = span_cluster_prefix
        self._rehearsal_model = None

        self.cfg = {}

    def predict(self, docs: Iterable[Doc]) -> List[MentionClusters]:
        """Apply the pipeline's model to a batch of docs, without modifying them.

        docs (Iterable[Doc]): The documents to predict.
        RETURNS: The models prediction for each document.

        DOCS: https://spacy.io/api/coref#predict (TODO)
        """
        out = []
        for doc in docs:
            scores, idxs = self.model.predict([doc])
            # idxs is a list of mentions (start / end idxs)
            # each item in scores includes scores and a mapping from scores to mentions
            ant_idxs = idxs

            # TODO batching
            xp = self.model.ops.xp

            starts = xp.arange(0, len(doc))
            ends = xp.arange(0, len(doc)) + 1

            predicted = get_predicted_clusters(xp, starts, ends, ant_idxs, scores)
            out.append(predicted)

        return out

    def set_annotations(self, docs: Iterable[Doc], clusters_by_doc) -> None:
        """Modify a batch of Doc objects, using pre-computed scores.

        docs (Iterable[Doc]): The documents to modify.
        clusters: The span clusters, produced by CoreferenceResolver.predict.

        DOCS: https://spacy.io/api/coref#set_annotations (TODO)
        """
        if len(docs) != len(clusters_by_doc):
            raise ValueError(
                "Found coref clusters incompatible with the "
                "documents provided to the 'coref' component. "
                "This is likely a bug in spaCy."
            )
        for doc, clusters in zip(docs, clusters_by_doc):
            for ii, cluster in enumerate(clusters):
                key = self.span_cluster_prefix + "_" + str(ii)
                if key in doc.spans:
                    raise ValueError(
                        "Found coref clusters incompatible with the "
                        "documents provided to the 'coref' component. "
                        "This is likely a bug in spaCy."
                    )

                doc.spans[key] = []
                for mention in cluster:
                    doc.spans[key].append(doc[mention[0] : mention[1]])

    def update(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Learn from a batch of documents and gold-standard information,
        updating the pipe's model. Delegates to predict and get_loss.

        examples (Iterable[Example]): A batch of Example objects.
        drop (float): The dropout rate.
        sgd (thinc.api.Optimizer): The optimizer.
        losses (Dict[str, float]): Optional record of the loss during training.
            Updated using the component name as the key.
        RETURNS (Dict[str, float]): The updated losses dictionary.

        DOCS: https://spacy.io/api/coref#update (TODO)
        """
        if losses is None:
            losses = {}
        losses.setdefault(self.name, 0.0)
        validate_examples(examples, "CoreferenceResolver.update")
        if not any(len(eg.predicted) if eg.predicted else 0 for eg in examples):
            # Handle cases where there are no tokens in any docs.
            return losses
        set_dropout_rate(self.model, drop)

        total_loss = 0

        for eg in examples:
            # TODO check this causes no issues (in practice it runs)
            preds, backprop = self.model.begin_update([eg.predicted])
            score_matrix, mention_idx = preds
            loss, d_scores = self.get_loss([eg], score_matrix, mention_idx)
            total_loss += loss
            # TODO check shape here
            backprop((d_scores, mention_idx))

        if sgd is not None:
            self.finish_update(sgd)
        losses[self.name] += total_loss
        return losses

    def rehearse(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Perform a "rehearsal" update from a batch of data. Rehearsal updates
        teach the current model to make predictions similar to an initial model,
        to try to address the "catastrophic forgetting" problem. This feature is
        experimental.

        examples (Iterable[Example]): A batch of Example objects.
        drop (float): The dropout rate.
        sgd (thinc.api.Optimizer): The optimizer.
        losses (Dict[str, float]): Optional record of the loss during training.
            Updated using the component name as the key.
        RETURNS (Dict[str, float]): The updated losses dictionary.

        DOCS: https://spacy.io/api/coref#rehearse (TODO)
        """
        if losses is not None:
            losses.setdefault(self.name, 0.0)
        if self._rehearsal_model is None:
            return losses
        validate_examples(examples, "CoreferenceResolver.rehearse")
        # TODO test this whole function
        docs = [eg.predicted for eg in examples]
        if not any(len(doc) for doc in docs):
            # Handle cases where there are no tokens in any docs.
            return losses
        set_dropout_rate(self.model, drop)
        scores, bp_scores = self.model.begin_update(docs)
        # TODO below
        target = self._rehearsal_model(examples)
        gradient = scores - target
        bp_scores(gradient)
        if sgd is not None:
            self.finish_update(sgd)
        if losses is not None:
            losses[self.name] += (gradient**2).sum()
        return losses

    def add_label(self, label: str) -> int:
        """Technically this method should be implemented from TrainablePipe,
        but it is not relevant for the coref component.
        """
        raise NotImplementedError(
            Errors.E931.format(
                parent="CoreferenceResolver", method="add_label", name=self.name
            )
        )

    def get_loss(
        self,
        examples: Iterable[Example],
        score_matrix: List[Tuple[Floats2d, Ints2d]],
        mention_idx: Ints2d,
    ):
        """Find the loss and gradient of loss for the batch of documents and
        their predicted scores.

        examples (Iterable[Examples]): The batch of examples.
        scores: Scores representing the model's predictions.
        RETURNS (Tuple[float, float]): The loss and the gradient.

        DOCS: https://spacy.io/api/coref#get_loss (TODO)
        """
        ops = self.model.ops
        xp = ops.xp

        # TODO if there is more than one example, give an error
        # (or actually rework this to take multiple things)
        example = examples[0]
        cscores = score_matrix
        cidx = mention_idx

        clusters = get_clusters_from_doc(example.reference)
        span_idxs = create_head_span_idxs(ops, len(example.predicted))
        gscores = create_gold_scores(span_idxs, clusters)
        gscores = ops.asarray2f(gscores)
        # top_gscores = xp.take_along_axis(gscores, cidx, axis=1)
        top_gscores = xp.take_along_axis(gscores, mention_idx, axis=1)
        # now add the placeholder
        gold_placeholder = ~top_gscores.any(axis=1).T
        gold_placeholder = xp.expand_dims(gold_placeholder, 1)
        top_gscores = xp.concatenate((gold_placeholder, top_gscores), 1)

        # boolean to float
        top_gscores = ops.asarray2f(top_gscores)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning)
            log_marg = ops.softmax(cscores + ops.xp.log(top_gscores), axis=1)
        log_norm = ops.softmax(cscores, axis=1)
        grad = log_norm - log_marg
        #gradients.append((grad, cidx))
        loss = float((grad**2).sum())

        return loss, grad

    def initialize(
        self,
        get_examples: Callable[[], Iterable[Example]],
        *,
        nlp: Optional[Language] = None,
    ) -> None:
        """Initialize the pipe for training, using a representative set
        of data examples.

        get_examples (Callable[[], Iterable[Example]]): Function that
            returns a representative sample of gold-standard Example objects.
        nlp (Language): The current nlp object the component is part of.

        DOCS: https://spacy.io/api/coref#initialize (TODO)
        """
        validate_get_examples(get_examples, "CoreferenceResolver.initialize")

        X = []
        Y = []
        for ex in islice(get_examples(), 2):
            X.append(ex.predicted)
            Y.append(ex.reference)

        assert len(X) > 0, Errors.E923.format(name=self.name)
        self.model.initialize(X=X, Y=Y)

    # TODO This mirrors the evaluation used in prior work, but we don't want to
    # include this in the final release. The metrics all have fundamental
    # issues and the current implementation requires scipy.
    def score(self, examples, **kwargs):
        """Score a batch of examples."""

        # NOTE traditionally coref uses the average of b_cubed, muc, and ceaf.
        # we need to handle the average ourselves.
        scores = []
        for metric in (b_cubed, muc, ceafe):
            evaluator = Evaluator(metric)

            for ex in examples:
                p_clusters = doc2clusters(ex.predicted, self.span_cluster_prefix)
                g_clusters = doc2clusters(ex.reference, self.span_cluster_prefix)
                cluster_info = get_cluster_info(p_clusters, g_clusters)
                evaluator.update(cluster_info)

            score = {
                "coref_f": evaluator.get_f1(),
                "coref_p": evaluator.get_precision(),
                "coref_r": evaluator.get_recall(),
            }
            scores.append(score)

        out = {}
        for field in ("f", "p", "r"):
            fname = f"coref_{field}"
            out[fname] = mean([ss[fname] for ss in scores])
        return out


default_span_predictor_config = """
[model]
@architectures = "spacy.SpanPredictor.v1"
hidden_size = 1024
dist_emb_size = 64

[model.tok2vec]
@architectures = "spacy.Tok2Vec.v2"

[model.tok2vec.embed]
@architectures = "spacy.MultiHashEmbed.v1"
width = 64
rows = [2000, 2000, 1000, 1000, 1000, 1000]
attrs = ["ORTH", "LOWER", "PREFIX", "SUFFIX", "SHAPE", "ID"]
include_static_vectors = false

[model.tok2vec.encode]
@architectures = "spacy.MaxoutWindowEncoder.v2"
width = ${model.tok2vec.embed.width}
window_size = 1
maxout_pieces = 3
depth = 2
"""
DEFAULT_SPAN_PREDICTOR_MODEL = Config().from_str(default_span_predictor_config)["model"]

@Language.factory(
        "span_predictor",
        assigns=["doc.spans"],
        requires=["doc.spans"],
        default_config={
            "model": DEFAULT_SPAN_PREDICTOR_MODEL,
            "input_prefix": "coref_head_clusters",
            "output_prefix": "coref_clusters",
            },
    default_score_weights={"span_predictor_f": 1.0, "span_predictor_p": None, "span_predictor_r": None},
    )
def make_span_predictor(
        nlp: Language,
        name: str,
        model,
        input_prefix: str = "coref_head_clusters",
        output_prefix: str = "coref_clusters",
) -> "SpanPredictor":
    """Create a SpanPredictor component."""
    return SpanPredictor(nlp.vocab, model, name, input_prefix=input_prefix, output_prefix=output_prefix)

class SpanPredictor(TrainablePipe):
    """Pipeline component to resolve one-token spans to full spans.

    Used in coreference resolution.
    """

    def __init__(
        self,
        vocab: Vocab,
        model: Model,
        name: str = "span_predictor",
        *,
        input_prefix: str = "coref_head_clusters",
        output_prefix: str = "coref_clusters",
    ) -> None:
        self.vocab = vocab
        self.model = model
        self.name = name
        self.input_prefix = input_prefix
        self.output_prefix = output_prefix

        self.cfg = {}

    def predict(self, docs: Iterable[Doc]) -> List[MentionClusters]:
        # for now pretend there's just one doc

        out = []
        for doc in docs:
            # TODO check shape here
            span_scores = self.model.predict([doc])
            if span_scores.size:
                # the information about clustering has to come from the input docs
                # first let's convert the scores to a list of span idxs
                start_scores = span_scores[:, :, 0]
                end_scores = span_scores[:, :, 1]
                starts = start_scores.argmax(axis=1)
                ends = end_scores.argmax(axis=1)

                # TODO check start < end

                # get the old clusters (shape will be preserved)
                clusters = doc2clusters(doc, self.input_prefix)
                cidx = 0
                out_clusters = []
                for cluster in clusters:
                    ncluster = []
                    for mention in cluster:
                        ncluster.append((starts[cidx], ends[cidx]))
                        cidx += 1
                    out_clusters.append(ncluster)
            else:
                out_clusters = []
            out.append(out_clusters)
        return out

    def set_annotations(self, docs: Iterable[Doc], clusters_by_doc) -> None:
        for doc, clusters in zip(docs, clusters_by_doc):
            for ii, cluster in enumerate(clusters):
                spans = [doc[mm[0]:mm[1]] for mm in cluster]
                doc.spans[f"{self.output_prefix}_{ii}"] = spans

    def update(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Learn from a batch of documents and gold-standard information,
        updating the pipe's model. Delegates to predict and get_loss.
        """
        if losses is None:
            losses = {}
        losses.setdefault(self.name, 0.0)
        validate_examples(examples, "SpanPredictor.update")
        if not any(len(eg.reference) if eg.reference else 0 for eg in examples):
            # Handle cases where there are no tokens in any docs.
            return losses
        set_dropout_rate(self.model, drop)

        total_loss = 0
        for eg in examples:
            # For update we use the gold coref_head_clusters
            # in the reference.
            span_scores, backprop = self.model.begin_update([eg.reference])
            loss, d_scores = self.get_loss([eg], span_scores)
            total_loss += loss
            # TODO check shape here
            backprop(d_scores)

        if sgd is not None:
            self.finish_update(sgd)
        losses[self.name] += total_loss
        return losses

    def rehearse(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        # TODO this should be added later
        raise NotImplementedError(
            Errors.E931.format(
                parent="SpanPredictor", method="add_label", name=self.name
            )
        )

    def add_label(self, label: str) -> int:
        """Technically this method should be implemented from TrainablePipe,
        but it is not relevant for this component.
        """
        raise NotImplementedError(
            Errors.E931.format(
                parent="SpanPredictor", method="add_label", name=self.name
            )
        )

    def get_loss(
        self,
        examples: Iterable[Example],
        span_scores: Floats3d,
    ):
        ops = self.model.ops

        # NOTE This is doing fake batching, and should always get a list of one example
        assert len(examples) == 1, "Only fake batching is supported."
        # starts and ends are gold starts and ends (Ints1d)
        # span_scores is a Floats3d. What are the axes? mention x token x start/end

        for eg in examples:
            starts = []
            ends = []
            for key, sg in eg.reference.spans.items():
                if key.startswith(self.output_prefix):
                    for mention in sg:
                        starts.append(mention.start)
                        ends.append(mention.end)

            starts = self.model.ops.xp.asarray(starts)
            ends = self.model.ops.xp.asarray(ends)
            start_scores = span_scores[:, :, 0]
            end_scores = span_scores[:, :, 1]
            n_classes = start_scores.shape[1]
            start_probs = ops.softmax(start_scores, axis=1)
            end_probs = ops.softmax(end_scores, axis=1)
            start_targets = to_categorical(starts, n_classes)
            end_targets = to_categorical(ends, n_classes)
            start_grads = (start_probs - start_targets)
            end_grads = (end_probs - end_targets)
            grads = ops.xp.stack((start_grads, end_grads), axis=2)
            loss = float((grads ** 2).sum())
        return loss, grads

    def initialize(
        self,
        get_examples: Callable[[], Iterable[Example]],
        *,
        nlp: Optional[Language] = None,
    ) -> None:
        validate_get_examples(get_examples, "SpanPredictor.initialize")

        X = []
        Y = []
        for ex in islice(get_examples(), 2):

            if not ex.predicted.spans:
                # set placeholder for shape inference
                doc = ex.predicted
                assert len(doc) > 2, "Coreference requires at least two tokens"
                doc.spans[f"{self.input_prefix}_0"] = [doc[0:1], doc[1:2]]
            X.append(ex.predicted)
            Y.append(ex.reference)

        assert len(X) > 0, Errors.E923.format(name=self.name)
        self.model.initialize(X=X, Y=Y)

    def score(self, examples, **kwargs):
        """Score a batch of examples."""
        # TODO This is basically the same as the main coref component - factor out?

        scores = []
        for metric in (b_cubed, muc, ceafe):
            evaluator = Evaluator(metric)

            for ex in examples:
                # XXX this is the only different part
                p_clusters = doc2clusters(ex.predicted, self.output_prefix)
                g_clusters = doc2clusters(ex.reference, self.output_prefix)
                cluster_info = get_cluster_info(p_clusters, g_clusters)

                evaluator.update(cluster_info)

            score = {
                "coref_span_f": evaluator.get_f1(),
                "coref_span_p": evaluator.get_precision(),
                "coref_span_r": evaluator.get_recall(),
            }
            scores.append(score)

        out = {}
        for field in ("f", "p", "r"):
            fname = f"coref_span_{field}"
            out[fname] = mean([ss[fname] for ss in scores])
        return out
