# flake8: noqa
import base64
import hashlib
import json
import logging
import os
import pickle
import sys
from io import BytesIO, StringIO

import pandas as pd
from PIL import Image

from autogluon.core.constants import QUANTILE, REGRESSION
from autogluon.core.utils import get_pred_from_proba_df
from autogluon.tabular import TabularPredictor

image_dir = os.path.join("/tmp", "ag_images")
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)


def _save_image_and_update_dataframe_column(bytes):
    os.makedirs(image_dir, exist_ok=True)
    im_bytes = base64.b85decode(bytes)
    # nosec B303 - not a cryptographic use
    im_hash = hashlib.sha1(im_bytes).hexdigest()
    im = Image.open(BytesIO(im_bytes))
    im_name = f"tabular_image_{im_hash}.png"
    im_path = os.path.join(image_dir, im_name)
    im.save(im_path)
    print(f"Image saved as {im_path}")

    return im_path


def model_fn(model_dir):
    """loads model from previously saved artifact"""
    logger.info("Loading the model")
    try:
        model = TabularPredictor.load(model_dir)
        model.persist_models()
        globals()["column_names"] = model.original_features
    except Exception as e:
        logger.error(f"Error loading the model: {str(e)}")
        raise e

    return model


def transform_fn(model, request_body, input_content_type, output_content_type="application/json"):
    inference_kwargs = os.environ.get("inference_kwargs", {})
    if inference_kwargs:
        inference_kwargs = json.loads(inference_kwargs)

    if input_content_type == "application/x-parquet":
        buf = BytesIO(request_body)
        data = pd.read_parquet(buf)

    elif input_content_type == "text/csv":
        buf = StringIO(request_body)
        data = pd.read_csv(buf)

    elif input_content_type == "application/json":
        buf = StringIO(request_body)
        data = pd.read_json(buf)

    elif input_content_type == "application/jsonl":
        buf = StringIO(request_body)
        data = pd.read_json(buf, orient="records", lines=True)

    elif input_content_type == "application/x-autogluon":
        buf = bytes(request_body)
        payload = pickle.loads(buf)
        data = pd.read_parquet(BytesIO(payload["data"]))
        inference_kwargs = payload.get("inference_kwargs", {})

    else:
        raise ValueError(f"{input_content_type} input content type not supported.")

    test_columns = sorted(list(data.columns))
    train_columns = sorted(column_names)
    if test_columns != train_columns:
        num_cols = len(data.columns)

        if num_cols != len(column_names):
            raise Exception(
                f"Invalid data format. Input data has {num_cols} while the model expects {len(column_names)}"
            )

        else:
            new_row = pd.DataFrame(data.columns).transpose()
            old_rows = pd.DataFrame(data.values)
            data = pd.concat([new_row, old_rows]).reset_index(drop=True)
            data.columns = column_names

    # find image column
    image_column = None
    for column_name, special_types in model.feature_metadata.get_type_map_special().items():
        if "image_path" in special_types:
            image_column = column_name
            break
    # save image column bytes to disk and update the column with saved path
    if image_column is not None:
        print(f"Detected image column {image_column}")
        data[image_column] = [_save_image_and_update_dataframe_column(bytes) for bytes in data[image_column]]

    if model.problem_type not in [REGRESSION, QUANTILE]:
        pred_proba = model.predict_proba(data, as_pandas=True, **inference_kwargs)
        pred = get_pred_from_proba_df(pred_proba, problem_type=model.problem_type)
        pred_proba.columns = [str(c) + "_proba" for c in pred_proba.columns]
        pred.name = model.label
        prediction = pd.concat([pred, pred_proba], axis=1)
    else:
        prediction = model.predict(data, as_pandas=True, **inference_kwargs)
    if isinstance(prediction, pd.Series):
        prediction = prediction.to_frame()

    if "application/x-parquet" in output_content_type:
        prediction.columns = prediction.columns.astype(str)
        output = prediction.to_parquet()
        output_content_type = "application/x-parquet"
    elif "application/json" in output_content_type:
        output = prediction.to_json()
        output_content_type = "application/json"
    elif "text/csv" in output_content_type:
        output = prediction.to_csv(index=None)
        output_content_type = "text/csv"
    else:
        raise ValueError(f"{output_content_type} content type not supported")

    return output, output_content_type
