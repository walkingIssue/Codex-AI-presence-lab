"""Split Kokoro at its quality-safe ALBERT-to-synthesis boundary.

The ALBERT text encoder is a single-input/single-output subgraph that runs
accurately on Intel Arc through native OpenVINO. Duration prediction, prosody,
and waveform synthesis remain in an untouched ONNX Runtime CPU graph so GPU
precision and partitioning cannot corrupt the audio seam.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import onnx
from onnx import utils


BERT_BOUNDARY = (
    "/encoder/bert/encoder/albert_layer_groups.0/albert_layers.0/"
    "full_layer_layer_norm_11/LayerNormalization_output_0"
)
MODEL_INPUTS = ["tokens", "style", "speed"]
TAIL_INPUTS = [BERT_BOUNDARY, *MODEL_INPUTS]


def validate_source(model: onnx.ModelProto) -> None:
    outputs = {
        output_name: node
        for node in model.graph.node
        for output_name in node.output
    }
    producer = outputs.get(BERT_BOUNDARY)
    if producer is None or producer.op_type != "LayerNormalization":
        raise ValueError("Kokoro ALBERT boundary was not found")
    consumers = [
        node for node in model.graph.node if BERT_BOUNDARY in node.input
    ]
    if len(consumers) != 1 or consumers[0].name != "/encoder/bert_encoder/MatMul":
        raise ValueError("Kokoro ALBERT boundary no longer has one known consumer")
    graph_inputs = {value.name for value in model.graph.input}
    if not set(MODEL_INPUTS).issubset(graph_inputs):
        raise ValueError("Kokoro model inputs changed")
    graph_outputs = {value.name for value in model.graph.output}
    if "audio" not in graph_outputs:
        raise ValueError("Kokoro audio output is missing")


def split_model(
    source: Path,
    bert_destination: Path,
    tail_destination: Path,
) -> tuple[int, int]:
    model = onnx.load(str(source), load_external_data=False)
    validate_source(model)
    bert_destination.parent.mkdir(parents=True, exist_ok=True)
    tail_destination.parent.mkdir(parents=True, exist_ok=True)

    utils.extract_model(
        str(source),
        str(bert_destination),
        ["tokens"],
        [BERT_BOUNDARY],
    )
    utils.extract_model(
        str(source),
        str(tail_destination),
        TAIL_INPUTS,
        ["audio"],
    )

    bert_model = onnx.load(str(bert_destination), load_external_data=False)
    tail_model = onnx.load(str(tail_destination), load_external_data=False)
    onnx.checker.check_model(bert_model)
    onnx.checker.check_model(tail_model)
    if any(node.op_type == "STFT" for node in bert_model.graph.node):
        raise ValueError("STFT unexpectedly crossed into the Arc text encoder")
    source_has_stft = any(node.op_type == "STFT" for node in model.graph.node)
    if source_has_stft and not any(
        node.op_type == "STFT" for node in tail_model.graph.node
    ):
        raise ValueError("The CPU synthesis tail no longer contains Kokoro STFT")
    return len(bert_model.graph.node), len(tail_model.graph.node)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("bert_destination", type=Path)
    parser.add_argument("tail_destination", type=Path)
    args = parser.parse_args()
    bert_nodes, tail_nodes = split_model(
        args.source,
        args.bert_destination,
        args.tail_destination,
    )
    print(
        f"split Kokoro into {bert_nodes}-node Arc ALBERT graph and "
        f"{tail_nodes}-node CPU synthesis graph"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
