"""Round-trip the firmware serializer through an independent JSON parser."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def serializer(tmp_path_factory):
    compiler = shutil.which("g++")
    assert compiler, "The firmware serializer regression requires g++"
    source = (ROOT / "esphome/components/runtime_diag/runtime_diag.cpp").read_text()
    start = source.index("void append_json_string(")
    end = source.index("\n}\n", start) + 2
    method = source[start:end]
    build = tmp_path_factory.mktemp("runtime-diag-json")
    cpp = build / "serializer.cpp"
    cpp.write_text(
        "#include <iostream>\n#include <sstream>\n#include <string>\n"
        + method
        + r'''
int main(int argc, char **argv) {
  if (argc != 2) return 2;
  const std::string hex = argv[1];
  std::string value;
  for (size_t i = 0; i < hex.size(); i += 2)
    value.push_back(static_cast<char>(std::stoul(hex.substr(i, 2), nullptr, 16)));
  std::ostringstream out;
  append_json_string(out, value);
  std::cout << "{\"reason\":" << out.str() << "}";
}
'''
    )
    binary = build / "serializer"
    result = subprocess.run(
        [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(binary)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize(
    "value",
    [chr(i) for i in range(32)] + ['"', "\\", "caffè e 音声", "ordinary snapshot", ""],
    ids=[f"control-{i:02x}" for i in range(32)] + ["quote", "backslash", "utf8", "ordinary", "empty"],
)
def test_firmware_snapshot_reason_round_trip(serializer, value):
    result = subprocess.run(
        [str(serializer), value.encode().hex()], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"reason": value}
