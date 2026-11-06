"""Real Windows processes exercise only file transport and read-only power queries."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from benchmarks.active_screen_energy import EnergyConditionError, require_energy
from benchmarks.sensitivity_controls import PWSH

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows I/O required")


def wait_file(path, process):
    deadline = time.monotonic() + 15
    while not path.exists():
        assert process.poll() is None, "producer stopped prematurely"
        assert time.monotonic() < deadline, "producer readiness timeout"
        time.sleep(0.01)


def test_real_concurrent_publication_and_producer_interruption(tmp_path):
    script = tmp_path / "publisher with spaces.ps1"
    script.write_text(
        f"""
Add-Type -Path '{ROOT / "scripts/ActiveScreenIO.cs"}'
$target='{tmp_path / "guard.json"}'
function Publish {{
 $s=@{{ready=$true;failure='';display=1;heartbeat_utc=[DateTime]::UtcNow.ToString('o')}}
 [ActiveScreenIO]::Publish($target,($s | ConvertTo-Json))
}}
Publish
[IO.File]::WriteAllText('{tmp_path / "ready"}','ready')
while(-not(Test-Path '{tmp_path / "start"}')){{Start-Sleep -Milliseconds 10}}
$clock=[Diagnostics.Stopwatch]::StartNew()
while($clock.Elapsed.TotalSeconds -lt 7){{Publish; Start-Sleep -Milliseconds 1}}
""",
        encoding="utf-8",
    )
    reader = tmp_path / "reader.py"
    reader.write_text(
        f"""
import sys,time,os
sys.path.insert(0,{str(ROOT)!r})
from benchmarks.active_screen_energy import require_energy
os.environ['FULFILLFLOW_ACTIVE_GUARD']={str(tmp_path / "guard.json")!r}
end=time.monotonic()+5
count=0
while time.monotonic()<end:
 require_energy(); count+=1
print(count)
""",
        encoding="utf-8",
    )
    producer = subprocess.Popen(
        [str(PWSH), "-NoProfile", "-File", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    readers = []
    try:
        wait_file(tmp_path / "ready", producer)
        readers = [
            subprocess.Popen(
                [sys.executable, str(reader)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            for _ in range(3)
        ]
        (tmp_path / "start").write_text("start")
        for p in readers:
            stdout, stderr = p.communicate(timeout=20)
            assert p.returncode == 0, stderr
            assert int(stdout) > 100
        _, stderr = producer.communicate(timeout=20)
        assert producer.returncode == 0, stderr
        # A stopped producer cannot refresh the evidence. No cached-read acceptance.
        env = dict(os.environ, FULFILLFLOW_ACTIVE_GUARD=str(tmp_path / "guard.json"))
        time.sleep(3.1)
        stale = subprocess.run(
            [
                sys.executable,
                "-c",
                "from benchmarks.active_screen_energy import require_energy; require_energy()",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert stale.returncode != 0 and b"freshness failed" in stale.stderr
    finally:
        for p in [producer, *readers]:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


def test_real_access_denied_and_atomic_replacement(tmp_path, monkeypatch):
    target = tmp_path / "guard.json"
    target.write_text(
        json.dumps(
            {
                "ready": True,
                "failure": "",
                "display": 1,
                "heartbeat_utc": "2000-01-01T00:00:00+00:00",
            }
        )
    )
    script = tmp_path / "held.ps1"
    script.write_text(
        f"""
Add-Type -Path '{ROOT / "scripts/ActiveScreenIO.cs"}'
$path='{target}'
$f=[IO.File]::Open($path,'Open','Read','None')
try {{
 [IO.File]::WriteAllText('{tmp_path / "ready"}','ready')
 try {{[ActiveScreenIO]::Publish($path,'{{"replacement":true}}');exit 9}}
 catch {{
  $code=$_.Exception.GetBaseException().NativeErrorCode.ToString()
  [IO.File]::WriteAllText('{tmp_path / "native"}',$code)
 }}
 while(-not(Test-Path '{tmp_path / "release"}')){{Start-Sleep -Milliseconds 10}}
}} finally {{$f.Dispose()}}
# A delete-sharing reader retains the old inode while new opens see the replacement.
$f=[IO.File]::Open($path,'Open','Read',([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
try {{
 [ActiveScreenIO]::Publish($path,'{{"replacement":true}}')
 $r=[IO.StreamReader]::new($f)
 [IO.File]::WriteAllText('{tmp_path / "old"}',$r.ReadToEnd())
}} finally {{$f.Dispose()}}
""",
        encoding="utf-8",
    )
    p = subprocess.Popen(
        [str(PWSH), "-NoProfile", "-File", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_file(tmp_path / "native", p)
        assert (tmp_path / "native").read_text() in {"5", "32"}
        monkeypatch.setenv("FULFILLFLOW_ACTIVE_GUARD", str(target))
        with pytest.raises(EnergyConditionError, match="winerror=32"):
            require_energy()
        (tmp_path / "release").write_text("release")
        _, stderr = p.communicate(timeout=10)
        assert p.returncode == 0, stderr
        assert json.loads((tmp_path / "old").read_text())["display"] == 1
        assert json.loads(target.read_text()) == {"replacement": True}
        with pytest.raises(EnergyConditionError, match="format failed"):
            require_energy()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_real_power_capture_changes_codepage_without_changing_settings(tmp_path):
    script = tmp_path / "capture.ps1"
    script.write_text(
        f"""
. '{ROOT / "scripts/ActiveScreenIO.ps1"}'
Add-Type @'
using System.Runtime.InteropServices;
public static class CP {{
 [DllImport("kernel32.dll")] public static extern bool SetConsoleOutputCP(uint n);
}}
'@
[CP]::SetConsoleOutputCP(850) | Out-Null
$a=Invoke-PowerCapture '/query' '{tmp_path / "first"}'
[CP]::SetConsoleOutputCP(65001) | Out-Null
$b=Invoke-PowerCapture '/query' '{tmp_path / "second"}'
if($a.Text -cne $b.Text){{exit 4}}
$r=Invoke-PowerCapture '/requests' '{tmp_path / "requests"}'
""",
        encoding="utf-8",
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    result = subprocess.run(
        [str(PWSH), "-NoProfile", "-File", str(script)],
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for name, cp in (("first", 850), ("second", 65001)):
        m = json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8-sig"))
        assert m["exit_code"] == 0 and m["code_page_before"] == m["code_page_after"] == cp
        raw = (tmp_path / f"{name}.stdout.bin").read_bytes()
        decoded = (tmp_path / f"{name}.txt").read_text(encoding="utf-8")
        assert raw.decode(f"cp{cp}").replace("\r\n", "\n") == decoded
        assert (tmp_path / f"{name}.stderr.bin").exists()
    assert (tmp_path / "first.txt").read_bytes() == (tmp_path / "second.txt").read_bytes()
    assert (tmp_path / "first.stdout.bin").read_bytes() != (
        tmp_path / "second.stdout.bin"
    ).read_bytes()


def test_mutex_timeout_and_interrupted_producer_fail_closed(tmp_path, monkeypatch):
    target = tmp_path / "guard.json"
    name = (
        "Local\\FulfillFlowHeartbeat"
        + hashlib.sha256(str(target).lower().encode("utf-8")).hexdigest()
    )
    script = tmp_path / "mutex.ps1"
    script.write_text(
        f"""
$m=[Threading.Mutex]::new($false,'{name}')
$m.WaitOne() | Out-Null
[IO.File]::WriteAllText('{tmp_path / "ready"}','ready')
Start-Sleep -Seconds 30
""",
        encoding="utf-8",
    )
    p = subprocess.Popen([str(PWSH), "-NoProfile", "-File", str(script)])
    try:
        wait_file(tmp_path / "ready", p)
        monkeypatch.setenv("FULFILLFLOW_ACTIVE_GUARD", str(target))
        with pytest.raises(EnergyConditionError, match="native_wait=258"):
            require_energy()
        # Only the synthetic producer is interrupted; no historical process is touched.
        p.kill()
        p.wait(timeout=5)
        with pytest.raises(EnergyConditionError):
            require_energy()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
