"""Tests for device tree overlay support (``/plugin/``)."""

import glob
import os
import shutil
import subprocess

import pytest

import fdt
from fdt.items import PropWords, Ref
from fdt.parser import DtsSyntaxError

DTC = shutil.which("dtc")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "overlay")
FIXTURES = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.dtbo")))
OVERLAY_EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "overlay-examples")

PLUGIN_DTS = """\
/dts-v1/;
/plugin/;

/ {
    fragment@0 {
        target = <&uart0>;
        __overlay__ {
            status = "okay";
        };
    };
};
"""

SUGAR_DTS = """\
/dts-v1/;
/plugin/;

&i2c8 {
    #address-cells = <1>;
    status = "okay";
    pcf8574: pcf8574@20 {
        compatible = "nxp,pcf8574";
        reg = <0x20>;
    };
};
"""

PATH_SUGAR_DTS = """\
/dts-v1/;
/plugin/;

&{/} {
    newthing {
        compatible = "x,y";
    };
};
"""

LOCAL_DTS = """\
/dts-v1/;
/plugin/;

/ {
    fragment@0 {
        target-path = "/";
        __overlay__ {
            link = <&mynode 0x2>;
            external = <&uart2>;
            mynode: testnode@1000 {
                reg = <0x1000 0x100>;
            };
        };
    };
};
"""

ROOT_PROP_DTS = """\
/dts-v1/;
/plugin/;
/ {
    compatible = "test,overlay";
    fragment@0 {
        target-path = "/";
        __overlay__ {
            x = <1>;
        };
    };
};
"""

CPP_MARKER_DTS = """\
# 0 "overlay.dts"
# 1 "overlay.dts"

/dts-v1/;
/plugin/;

&ocp {
    bar {
        compatible = "corp,bar";
    };
};
"""


def _compile(tmp_path, text, extra=()):
    src = os.path.join(str(tmp_path), "in.dts")
    out = os.path.join(str(tmp_path), "out.dtbo")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(text)
    subprocess.run(["dtc", *extra, "-I", "dts", "-O", "dtb", "-o", out, src],
                   check=True, capture_output=True)
    with open(out, "rb") as handle:
        return handle.read()


# ---------------------------------------------------------------- references

def test_ref_renders_label_path_and_value():
    assert Ref(label="foo").to_dts() == "&foo"
    assert Ref(path="/a/b").to_dts() == "&{/a/b}"
    assert Ref(value=0x12).to_dts() == "0x12"


def test_propwords_accepts_mixed_cells_and_roundtrips():
    prop = PropWords("x", 0x2, Ref(label="saradc"), Ref(path="/a/b"), 0x1)
    assert prop.to_dts().strip() == "x = <0x2 &saradc &{/a/b} 0x1>;"
    blob, _, _ = prop.to_dtb("")
    # the unresolved reference serializes as the placeholder phandle
    assert int.from_bytes(blob[16:20], "big") == 0xFFFFFFFF
    assert prop.copy() == prop


def test_propwords_value_reads_through_reference():
    prop = PropWords("x", Ref(label="gpio3", value=7), 0xf)
    assert prop.value == 7
    blob, _, _ = prop.to_dtb("")
    assert int.from_bytes(blob[12:16], "big") == 7


def test_binary_parsed_trees_have_no_references():
    with open(os.path.join(DATA_DIR, "imx7d-sdb.dtb"), "rb") as handle:
        tree = fdt.parse_dtb(handle.read())
    for _, _, props in tree.walk():
        for prop in props:
            if isinstance(prop, PropWords):
                assert all(not isinstance(cell, Ref) for cell in prop.data)


def test_base_dtb_roundtrip_is_byte_identical():
    with open(os.path.join(DATA_DIR, "imx7d-sdb.dtb"), "rb") as handle:
        blob = handle.read()
    assert fdt.parse_dtb(blob).to_dtb() == blob


# ------------------------------------------------------------------- parsing

def test_parse_overlay_source_label_sugar():
    tree = fdt.parse_dts(SUGAR_DTS)
    assert tree.overlay is not None
    assert len(tree.overlay.fragments) == 1
    fragment = tree.overlay.fragments[0]
    assert fragment.target_name() == "&i2c8"
    assert fragment.body.get_subnode("pcf8574@20") is not None
    assert "/plugin/;" in tree.to_dts()
    assert "&i2c8 {" in tree.to_dts()


def test_parse_overlay_source_path_sugar():
    tree = fdt.parse_dts(PATH_SUGAR_DTS)
    assert tree.overlay.fragments[0].target_path == "/"
    assert "&{/} {" in tree.to_dts()


def test_parse_base_tree_has_no_overlay():
    with open(os.path.join(DATA_DIR, "addresses.dts")) as handle:
        tree = fdt.parse_dts(handle.read())
    assert tree.overlay is None


def test_detect_target_path_only_overlay():
    blob = fdt.parse_dts(PATH_SUGAR_DTS).to_dtb()
    tree = fdt.parse_dtb(blob)
    assert tree.overlay is not None
    assert tree.overlay.fragments[0].target_path == "/"


def test_parse_base_dtb_not_an_overlay():
    with open(os.path.join(DATA_DIR, "imx7d-sdb.dtb"), "rb") as handle:
        tree = fdt.parse_dtb(handle.read())
    assert tree.overlay is None


def test_symbolic_references_survive_dtb_parse():
    tree = fdt.parse_dts(LOCAL_DTS)
    blob = tree.to_dtb()
    again = fdt.parse_dtb(blob)
    assert again.overlay is not None
    # the overlay model carries the internal reference symbolically
    body = again.overlay.fragments[0].body
    link_prop = body.get_property("link")
    assert isinstance(link_prop.data[0], Ref)
    assert link_prop.data[0].label == "mynode"


# --------------------------------------------------- programmatic build

def test_programmatic_overlay_construction():
    tree = fdt.FDT()
    tree.header.version = 17

    model = fdt.Overlay()
    fragment = fdt.Fragment(target_path="/", index=0)
    panel = fragment.body.get_node("panel@0", create=True)
    panel.set_property("reset-gpios", [fdt.Ref(label="gpio3"), 0x4, 0])
    model.fragments.append(fragment)
    tree.overlay = model

    fdt.finalize(tree)
    blob = tree.to_dtb()

    assert "&gpio3" in tree.to_dts()

    again = fdt.parse_dtb(blob)
    assert again.overlay is not None
    assert again.overlay.fragments[0].target_path == "/"

    fixups = fdt.overlay.project(again).get_subnode("__fixups__")
    entry = fixups.get_property("gpio3")
    assert entry is not None
    assert list(entry.data) == ["/fragment@0/__overlay__/panel@0:reset-gpios:0"]


# ----------------------------------------------------------- preprocessing

def test_cpp_line_markers_ignored():
    tree = fdt.parse_dts(CPP_MARKER_DTS)
    assert tree.overlay is not None
    assert tree.overlay.fragments[0].body.get_subnode("bar") is not None


def test_unresolved_include_reports_error():
    with pytest.raises(DtsSyntaxError):
        fdt.parse_dts('#include <dt-bindings/gpio/gpio.h>\n/dts-v1/;\n')


def test_unresolved_macro_reports_error():
    with pytest.raises(DtsSyntaxError):
        fdt.parse_dts("/dts-v1/;\n/plugin/;\n&i2c8 { x = <RK_PD0>; };\n")


# ------------------------------------------------------------------ compile

@pytest.mark.skipif(DTC is None, reason="dtc is required")
@pytest.mark.parametrize("source", [SUGAR_DTS, PATH_SUGAR_DTS, LOCAL_DTS, PLUGIN_DTS])
def test_compile_matches_dtc(tmp_path, source):
    assert fdt.parse_dts(source).to_dtb() == _compile(tmp_path, source, extra=["-@"])


@pytest.mark.skipif(DTC is None, reason="dtc is required")
def test_compile_matches_dtc_for_examples(tmp_path):
    paths = sorted(glob.glob(os.path.join(OVERLAY_EXAMPLES, "**", "*.dts"), recursive=True))
    checked = 0
    for path in paths:
        if path.endswith(".unpack.dts"):
            continue
        try:
            with open(path) as handle:
                source = handle.read()
            expected = _compile(tmp_path, source, extra=["-@"])
        except subprocess.CalledProcessError:
            continue  # needs cpp preprocessing (kernel headers / macros)
        assert fdt.parse_dts(source).to_dtb() == expected, path
        checked += 1
    assert checked > 0


def test_compile_emits_external_and_local_fixups():
    tree = fdt.parse_dts(LOCAL_DTS)
    assert tree.overlay.fixups_node.get_property("uart2") is not None
    assert tree.overlay.local_fixups_node is not None


def test_symbols_emitted_only_when_labels_exist():
    # a labelled node produces __symbols__
    with_labels = fdt.parse_dts(SUGAR_DTS).to_dtb()
    assert fdt.parse_dtb(with_labels).overlay.symbols_node is not None
    # no labels -> no __symbols__ node
    assert b"__symbols__" not in fdt.parse_dts(PATH_SUGAR_DTS).to_dtb()


PHANDLE_ORDER_DTS = """\
/dts-v1/;
/plugin/;

/ {
    refs = <&c &a>;
    a: na {
    };
    b: nb {
    };
    c: nc {
    };
};
"""


@pytest.mark.skipif(DTC is None, reason="dtc is required")
def test_phandle_order_matches_dtc(tmp_path):
    tree = fdt.parse_dts(PHANDLE_ORDER_DTS)
    assert tree.get_node("/na").get_property("phandle").value == 0x02
    assert tree.get_node("/nb").get_property("phandle").value == 0x03
    assert tree.get_node("/nc").get_property("phandle").value == 0x01
    assert tree.to_dtb() == _compile(tmp_path, PHANDLE_ORDER_DTS, extra=["-@"])


# ---------------------------------------------------------------- decompile

def test_unpack_sugar_hides_metadata():
    for path in FIXTURES:
        with open(path, "rb") as handle:
            tree = fdt.parse_dtb(handle.read())
        dts = tree.to_dts()
        assert "/plugin/;" in dts
        assert "fragment@" not in dts
        assert "__symbols__" not in dts
        assert "__fixups__" not in dts
        assert "__local_fixups__" not in dts
        assert "&{" in dts or "&" in dts


def test_fallback_when_sugar_not_representable():
    blob = fdt.parse_dts(ROOT_PROP_DTS).to_dtb()
    tree = fdt.parse_dtb(blob)
    dts = tree.to_dts()
    assert "fragment@0" in dts
    assert "/plugin/;" in dts


# --------------------------------------------------------------- round trips

@pytest.mark.parametrize("path", FIXTURES)
def test_decode_encode_identity(path):
    with open(path, "rb") as handle:
        blob = handle.read()
    assert fdt.parse_dtb(blob).to_dtb() == blob


@pytest.mark.skipif(DTC is None, reason="dtc is required")
def test_text_roundtrip_dtc_compiled(tmp_path):
    for source in (SUGAR_DTS, PATH_SUGAR_DTS, LOCAL_DTS, PLUGIN_DTS):
        blob = _compile(tmp_path, source, extra=["-@"])
        dts = fdt.parse_dtb(blob).to_dts()
        assert fdt.parse_dts(dts).to_dtb() == blob


def test_fixtures_are_sugar_eligible():
    assert FIXTURES
    for path in FIXTURES:
        with open(path, "rb") as handle:
            tree = fdt.parse_dtb(handle.read())
        assert tree.overlay is not None
        assert tree.overlay.fragments
