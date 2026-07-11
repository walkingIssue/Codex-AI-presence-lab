"""Wrap rank-3 ConvTranspose nodes as equivalent rank-4 operations.

The ONNX Runtime DirectML provider rejects Kokoro's grouped 1-D transposed
convolutions on this machine.  A height-one 2-D convolution is mathematically
equivalent and avoids that provider-specific validation path.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper


def initializer_values(model: onnx.ModelProto) -> dict[str, np.ndarray]:
    return {
        initializer.name: numpy_helper.to_array(initializer)
        for initializer in model.graph.initializer
    }


def rank_of(value_info: onnx.ValueInfoProto | None) -> int | None:
    if value_info is None or not value_info.type.HasField("tensor_type"):
        return None
    tensor_type = value_info.type.tensor_type
    if not tensor_type.HasField("shape"):
        return None
    return len(tensor_type.shape.dim)


def replace_ints(node: onnx.NodeProto, name: str, values: list[int]) -> None:
    kept = [attribute for attribute in node.attribute if attribute.name != name]
    node.ClearField("attribute")
    node.attribute.extend(kept)
    node.attribute.append(helper.make_attribute(name, values))


def patch_model(
    source: Path,
    destination: Path,
    targets: set[str] | None = None,
) -> int:
    model = onnx.load(str(source), load_external_data=False)
    values = initializer_values(model)
    value_infos = {
        value_info.name: value_info
        for value_info in list(model.graph.input)
        + list(model.graph.value_info)
        + list(model.graph.output)
    }
    axes_name = "/codex_voice/dml_convtranspose_axes"
    if not any(initializer.name == axes_name for initializer in model.graph.initializer):
        model.graph.initializer.append(
            numpy_helper.from_array(np.asarray([2], dtype=np.int64), axes_name)
        )

    patched_nodes: list[onnx.NodeProto] = []
    patched = 0

    for node in model.graph.node:
        weight = values.get(node.input[1]) if len(node.input) > 1 else None
        data_rank = rank_of(value_infos.get(node.input[0]))
        should_patch = (
            node.op_type == "ConvTranspose"
            and weight is not None
            and weight.ndim == 3
            # A few intermediate tensors in this export have an empty
            # ValueInfo shape even though they are runtime rank-3 tensors.
            # The rank-3 weight is therefore the reliable discriminator.
            and data_rank in (None, 0, 3)
            and (targets is None or node.name in targets)
        )
        if not should_patch:
            patched_nodes.append(node)
            continue

        attrs = {
            attribute.name: helper.get_attribute_value(attribute)
            for attribute in node.attribute
        }
        if attrs.get("auto_pad", b"NOTSET") != b"NOTSET":
            raise ValueError(f"Expected explicit pads for {node.name}")

        kernel = list(attrs.get("kernel_shape", [weight.shape[-1]]))
        strides = list(attrs.get("strides", [1]))
        dilations = list(attrs.get("dilations", [1]))
        pads = list(attrs.get("pads", [0, 0]))
        output_padding = list(attrs.get("output_padding", [0]))
        if not all(len(values_) == 1 for values_ in (kernel, strides, dilations)):
            raise ValueError(f"Expected 1-D convolution attributes for {node.name}")
        if len(pads) != 2 or len(output_padding) != 1:
            raise ValueError(f"Unexpected padding attributes for {node.name}")

        data_name = node.input[0]
        original_output_name = node.output[0]
        unsqueezed_data = f"{node.name}/dml_unsqueezed_data"
        resized_output = f"{node.name}/dml_2d_output"
        weight_name = node.input[1]
        expanded_weight_name = f"{node.name}/dml_weight_2d"

        expanded_weight = np.expand_dims(weight, axis=2)
        model.graph.initializer.append(
            numpy_helper.from_array(expanded_weight, expanded_weight_name)
        )

        patched_nodes.append(
            helper.make_node(
                "Unsqueeze",
                [data_name, axes_name],
                [unsqueezed_data],
                name=f"{node.name}/dml_unsqueeze_data",
            )
        )

        node.input[0] = unsqueezed_data
        node.input[1] = expanded_weight_name
        node.output[0] = resized_output
        replace_ints(node, "kernel_shape", [1, kernel[0]])
        replace_ints(node, "strides", [1, strides[0]])
        replace_ints(node, "dilations", [1, dilations[0]])
        replace_ints(node, "pads", [0, pads[0], 0, pads[1]])
        if "output_padding" in attrs:
            replace_ints(node, "output_padding", [0, output_padding[0]])
        if "output_shape" in attrs:
            output_shape = list(attrs["output_shape"])
            if len(output_shape) != 1:
                raise ValueError(f"Unexpected output_shape for {node.name}")
            replace_ints(node, "output_shape", [1, output_shape[0]])
        patched_nodes.append(node)
        patched_nodes.append(
            helper.make_node(
                "Squeeze",
                [resized_output, axes_name],
                [original_output_name],
                name=f"{node.name}/dml_squeeze_output",
            )
        )
        patched += 1

    if targets is not None:
        found = {
            node.name
            for node in model.graph.node
            if node.op_type == "ConvTranspose" and node.name in targets
        }
        if found != targets:
            raise ValueError(f"Target nodes not found: {sorted(targets - found)}")

    model.graph.ClearField("node")
    model.graph.node.extend(patched_nodes)
    used_inputs = {
        input_name
        for node in model.graph.node
        for input_name in node.input
        if input_name
    }
    kept_initializers = [
        initializer
        for initializer in model.graph.initializer
        if initializer.name in used_inputs
    ]
    model.graph.ClearField("initializer")
    model.graph.initializer.extend(kept_initializers)
    onnx.checker.check_model(model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    onnx.save_model(model, str(destination), save_as_external_data=False)
    return patched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--node", action="append", dest="nodes")
    args = parser.parse_args()
    count = patch_model(
        args.source,
        args.destination,
        set(args.nodes) if args.nodes else None,
    )
    print(f"patched {count} ConvTranspose nodes -> {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
