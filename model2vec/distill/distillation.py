from __future__ import annotations

import logging
import os
import re
from typing import Literal, Union

import numpy as np
from huggingface_hub import model_info
from sklearn.decomposition import PCA
from tokenizers import Tokenizer
from tokenizers.models import BPE, Unigram
from transformers import AutoModel, AutoTokenizer, PreTrainedModel, PreTrainedTokenizerFast

from model2vec.distill.inference import (
    create_output_embeddings_from_model,
    create_output_embeddings_from_model_and_tokens,
)
from model2vec.distill.tokenizer import add_tokens, preprocess_vocabulary, remove_tokens
from model2vec.distill.utils import select_optimal_device
from model2vec.model import StaticModel

try:
    # For huggingface_hub>=0.25.0
    from huggingface_hub.errors import RepositoryNotFoundError
except ImportError:
    # For huggingface_hub<0.25.0
    from huggingface_hub.utils._errors import RepositoryNotFoundError


logger = logging.getLogger(__name__)


PCADimType = Union[int, None, float, Literal["auto"]]


def distill_from_model(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizerFast,
    instruction: str | None = None,  # NOTE: Added this parameter
    vocabulary: list[str] | None = None,
    device: str | None = None,
    pca_dims: PCADimType = 256,
    apply_zipf: bool = True,
    use_subword: bool = True,
    token_remove_pattern: str | None = r"\[unused\d+\]",
) -> StaticModel:
    """
    Distill a staticmodel from a sentence transformer.

    This function creates a set of embeddings from a sentence transformer. It does this by doing either
    a forward pass for all subword tokens in the tokenizer, or by doing a forward pass for all tokens in a passed vocabulary.

    If you pass through a vocabulary, we create a custom word tokenizer for that vocabulary.
    If you don't pass a vocabulary, we use the model's tokenizer directly.

    :param model: The model to use.
    :param tokenizer: The tokenizer to use.
    :param instruction: The instruction to use.
    :param vocabulary: The vocabulary to use. If this is None, we use the model's vocabulary.
    :param device: The device to use.
    :param pca_dims: The number of components to use for PCA.
        If this is None, we don't apply PCA.
        If this is 'auto', we don't reduce dimensionality, but still apply PCA.
    :param apply_zipf: Whether to apply Zipf weighting to the embeddings.
    :param use_subword: Whether to keep subword tokens in the vocabulary. If this is False, you must pass a vocabulary, and the returned tokenizer will only detect full words.
    :param token_remove_pattern: If this is set to a string, we compile this into a regex. Any tokens that conform to this regex pattern will be removed from the vocabulary.
        If the pattern is so general that it removes all tokens, we throw an error. If the pattern can't be compiled into a valid regex, we also throw an error.
    :raises: ValueError if the PCA dimension is larger than the number of dimensions in the embeddings.
    :raises: ValueError if the vocabulary contains duplicate tokens.
    :raises: ValueError if the regex can't be compiled.
    :raises: ValueError if the vocabulary is empty after token removal.
    :return: A StaticModel

    """
    device = select_optimal_device(device)
    if not use_subword and vocabulary is None:
        raise ValueError(
            "You must pass a vocabulary if you don't use subword tokens. Either pass a vocabulary, or set use_subword to True."
        )

    if vocabulary and isinstance(tokenizer.backend_tokenizer.model, (BPE, Unigram)):
        raise ValueError(
            "You passed a vocabulary, but the model you are using does not use a WordPiece tokenizer. "
            "This is not supported yet."
            "Feel free to open an issue if this is a blocker: https://github.com/MinishLab/model2vec/issues"
        )

    # Make a base list of tokens.
    tokens: list[str] = []
    if use_subword:
        # Create the subword embeddings.
        tokens, embeddings = create_output_embeddings_from_model(model=model, tokenizer=tokenizer, device=device, instruction=instruction)  # NOTE: Added this parameter
        new_tokenizer, embeddings = _remove_tokens_and_embeddings(tokenizer, token_remove_pattern, tokens, embeddings)
    else:
        # We need to keep the unk token in the tokenizer.
        unk_token = tokenizer.backend_tokenizer.model.unk_token
        # Remove all tokens except the UNK token.
        new_tokenizer = remove_tokens(tokenizer.backend_tokenizer, list(set(tokenizer.get_vocab()) - {unk_token}))
        # We need to set embeddings to None because we don't know the dimensions of the embeddings yet.
        embeddings = None

    if vocabulary:
        # Preprocess the vocabulary with the original tokenizer.
        preprocessed_vocabulary = preprocess_vocabulary(tokenizer.backend_tokenizer, vocabulary)
        n_tokens_before = len(preprocessed_vocabulary)
        # Clean the vocabulary by removing duplicate tokens and tokens that are in the subword vocabulary.
        cleaned_vocabulary = _clean_vocabulary(preprocessed_vocabulary, tokens)
        n_tokens_after = len(cleaned_vocabulary)
        logger.info(
            f"Adding {n_tokens_after} tokens to the vocabulary. Removed {n_tokens_before - n_tokens_after} tokens during preprocessing."
        )
        # Only create embeddings if we have tokens to add.
        if cleaned_vocabulary:
            # Create the embeddings.
            _, token_embeddings = create_output_embeddings_from_model_and_tokens(
                model=model,
                tokenizer=tokenizer,
                tokens=cleaned_vocabulary,
                device=device
            )

            # If we don't have subword tokens, we still need to create
            #  some embeddings for [UNK] and some other special tokens.
            if embeddings is None:
                embeddings = np.zeros((new_tokenizer.get_vocab_size(), token_embeddings.shape[1]))
            embeddings = np.concatenate([embeddings, token_embeddings], axis=0)
            # Add the cleaned vocabulary to the tokenizer.
            new_tokenizer = add_tokens(new_tokenizer, cleaned_vocabulary)
        else:
            logger.warning("Didn't create any token embeddings as all tokens were duplicates or empty.")

    # Post process the embeddings by applying PCA and Zipf weighting.
    embeddings = _post_process_embeddings(np.asarray(embeddings), pca_dims, apply_zipf)

    model_name = getattr(model, "name_or_path", "")

    config = {
        "model_type": "model2vec",
        "architectures": ["StaticModel"],
        "tokenizer_name": model_name,
        "apply_pca": pca_dims,
        "apply_zipf": apply_zipf,
        "hidden_dim": embeddings.shape[1],
        "seq_length": 1000000,  # Set this to a high value since we don't have a sequence length limit.
    }

    if os.path.exists(model_name):
        # Using a local model. Get the model name from the path.
        model_name = os.path.basename(model_name)
        language = None
    else:
        # Get the language from the model card.
        try:
            info = model_info(model_name)
            language = info.cardData.get("language", None)
        except RepositoryNotFoundError:
            logger.info("No model info found for the model. Setting language to None.")
            language = None

    return StaticModel(
        vectors=embeddings, tokenizer=new_tokenizer, config=config, base_model_name=model_name, language=language
    )


def _remove_tokens_and_embeddings(
    tokenizer: PreTrainedTokenizerFast, token_remove_pattern: str | None, tokens: list[str], embeddings: np.ndarray
) -> tuple[Tokenizer, np.ndarray]:
    if not token_remove_pattern:
        return tokenizer.backend_tokenizer, embeddings

    try:
        token_regex = re.compile(token_remove_pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {token_remove_pattern}") from e
        # Remove any unused tokens from the tokenizer and embeddings.
    wrong_tokens = [x for x in tokens if token_regex.match(x)]
    vocab = tokenizer.get_vocab()
    # Get the ids of the unused token.
    wrong_token_ids = [vocab[token] for token in wrong_tokens]

    if len(wrong_token_ids) == len(vocab):
        raise ValueError(
            "All tokens in the vocabulary are unused tokens. This will result in an empty tokenizer. "
            "Please provide a valid token removal pattern. The pattern is now: {token_remove_pattern}"
        )

        # Remove the unused tokens from the tokenizer.
    new_tokenizer = remove_tokens(tokenizer.backend_tokenizer, wrong_tokens)
    if new_tokenizer.get_vocab_size() == tokenizer.backend_tokenizer.get_vocab_size():
        # This happens if we didn't remove any tokens.
        return new_tokenizer, embeddings

    # Remove the embeddings of the unused tokens.
    embeddings = np.delete(embeddings, wrong_token_ids, axis=0)
    logger.info(f"Removed {len(wrong_tokens)} unused tokens from the tokenizer and embeddings.")

    return new_tokenizer, embeddings


def distill(
    model_name: str,
    vocabulary: list[str] | None = None,
    instruction: str | None = None,
    device: str | None = None,
    pca_dims: PCADimType = 256,
    apply_zipf: bool = True,
    use_subword: bool = True,
    token_remove_pattern: str | None = r"\[unused\d+\]",
) -> StaticModel:
    """
    Distill a staticmodel from a sentence transformer.

    This function creates a set of embeddings from a sentence transformer. It does this by doing either
    a forward pass for all subword tokens in the tokenizer, or by doing a forward pass for all tokens in a passed vocabulary.

    If you pass through a vocabulary, we create a custom word tokenizer for that vocabulary.
    If you don't pass a vocabulary, we use the model's tokenizer directly.

    :param model_name: The model name to use. Any sentencetransformer compatible model works.
    :param vocabulary: The vocabulary to use. If this is None, we use the model's vocabulary.
    :param device: The device to use.
    :param pca_dims: The number of components to use for PCA.
        If this is None, we don't apply PCA.
        If this is 'auto', we don't reduce dimenionality, but still apply PCA.
    :param apply_zipf: Whether to apply Zipf weighting to the embeddings.
    :param use_subword: Whether to keep subword tokens in the vocabulary. If this is False, you must pass a vocabulary, and the returned tokenizer will only detect full words.
    :param token_remove_pattern: If this is set to a string, we compile this into a regex. Any tokens that conform to this regex pattern will be removed from the vocabulary.
    :return: A StaticModel

    """
    model: PreTrainedModel = AutoModel.from_pretrained(model_name)
    tokenizer: PreTrainedTokenizerFast = AutoTokenizer.from_pretrained(model_name)

    return distill_from_model(
        model=model,
        tokenizer=tokenizer,
        instruction=instruction,
        vocabulary=vocabulary,
        device=device,
        pca_dims=pca_dims,
        apply_zipf=apply_zipf,
        use_subword=use_subword,
        token_remove_pattern=token_remove_pattern,
    )


def _post_process_embeddings(embeddings: np.ndarray, pca_dims: PCADimType, apply_zipf: bool) -> np.ndarray:
    """Post process embeddings by applying PCA and Zipf weighting."""
    if pca_dims is not None:
        if pca_dims == "auto":
            pca_dims = embeddings.shape[1]
        if pca_dims > embeddings.shape[1]:
            logger.warning(
                f"PCA dimension ({pca_dims}) is larger than the number of dimensions in the embeddings ({embeddings.shape[1]}). "
                "Applying PCA, but not reducing dimensionality. Is this is not desired, please set `pca_dims` to None. "
                "Applying PCA will probably improve performance, so consider just leaving it."
            )
            pca_dims = embeddings.shape[1]
        if pca_dims >= embeddings.shape[0]:
            logger.warning(
                f"PCA dimension ({pca_dims}) is larger than the number of tokens in the vocabulary ({embeddings.shape[0]}). Not applying PCA."
            )
        elif pca_dims <= embeddings.shape[1]:
            if isinstance(pca_dims, float):
                logger.info(f"Applying PCA with {pca_dims} explained variance.")
            else:
                logger.info(f"Applying PCA with n_components {pca_dims}")

            orig_dims = embeddings.shape[1]
            p = PCA(n_components=pca_dims, svd_solver="full")
            embeddings = p.fit_transform(embeddings)

            if embeddings.shape[1] < orig_dims:
                explained_variance_ratio = np.sum(p.explained_variance_ratio_)
                explained_variance = np.sum(p.explained_variance_)
                logger.info(f"Reduced dimensionality from {orig_dims} to {embeddings.shape[1]}.")
                logger.info(f"Explained variance ratio: {explained_variance_ratio:.3f}.")
                logger.info(f"Explained variance: {explained_variance:.3f}.")

    if apply_zipf:
        logger.info("Applying Zipf weighting")
        embeddings *= np.log(1 + np.arange(embeddings.shape[0]))[:, None]

    return embeddings


def _clean_vocabulary(preprocessed_vocabulary: list[str], added_tokens: list[str]) -> list[str]:
    """Cleans a vocabulary by removing duplicates and tokens that were already in the vocabulary."""
    added_tokens_set = set(added_tokens)
    seen_tokens = set()
    cleaned_vocabulary = []
    n_empty = 0
    n_duplicates = 0
    for token in preprocessed_vocabulary:
        if not token:
            n_empty += 1
            continue
        if token in seen_tokens or token in added_tokens_set:
            n_duplicates += 1
            continue
        seen_tokens.add(token)
        cleaned_vocabulary.append(token)

    if n_duplicates:
        logger.warning(f"Removed {n_duplicates} duplicate tokens.")
    if n_empty:
        logger.warning(f"Removed {n_empty} empty tokens.")

    return cleaned_vocabulary
