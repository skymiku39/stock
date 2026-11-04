"""Exercise task-plan generation without altering Windows tasks or credentials."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt' or not shutil.which('powershell.exe'),
                              reason='Windows PowerShell installer')
INSTALLER = Path(__file__).resolve().parents[1] / 'scripts/install_stock_background.ps1'
NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}


def invoke(tmp_path, mode=None, *, original='InteractiveToken', apply=False):
    root = tmp_path / 'stock & data'
    python = root / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    output = tmp_path / 'plans'
    harness = tmp_path / 'preview.ps1'
    harness.write_text('''param([string]$Installer,[string]$Root,[string]$Output,[string]$Mode,[string]$Original,[switch]$Apply)
$ErrorActionPreference='Stop'
$global:StockFixtureXml = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Principals><Principal><UserId>FIXTURE\\user</UserId><LogonType>' + $Original + '</LogonType></Principal></Principals></Task>'
function Get-ScheduledTask { param($TaskName,$ErrorAction) if ($TaskName -eq 'SkyMiku Stock Watchdog') { return $null }; return [pscustomobject]@{TaskName=$TaskName} }
function Export-ScheduledTask { param($TaskName) return $global:StockFixtureXml }
$options=@{Root=$Root;OutputDirectory=$Output}
if ($Mode) { $options.LogonMode=$Mode }
if ($Apply) { $options.Apply=$true }
& $Installer @options
exit $LASTEXITCODE
''', encoding='utf-8-sig')
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
               '-ExecutionPolicy', 'Bypass', '-File', str(harness), '-Installer', str(INSTALLER),
               '-Root', str(root), '-Output', str(output), '-Original', original]
    if mode:
        command += ['-Mode', mode]
    if apply:
        command += ['-Apply']
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8-sig',
                          errors='replace', timeout=30), output, root


@pytest.mark.parametrize('mode,expected', [(None, 'Password'), ('Password', 'Password'), ('S4U', 'S4U')])
def test_preview_keeps_boot_and_recovery_in_the_selected_logon_mode(tmp_path, mode, expected):
    process, output, root = invoke(tmp_path, mode)
    assert process.returncode == 0, process.stderr
    result = json.loads((output / 'result.json').read_text(encoding='utf-8-sig'))
    assert result['status'] == 'preview' and result['logon_type'] == expected
    assert result['credentials_saved_by'] == ('No password stored' if expected == 'S4U'
                                               else 'Windows Task Scheduler only')
    plans = list(output.glob('*.proposed.xml'))
    assert len(plans) == 3
    for plan in plans:
        task = ET.parse(plan).getroot()
        assert task.findtext('t:Principals/t:Principal/t:LogonType', namespaces=NS) == expected
        assert task.findtext('t:Principals/t:Principal/t:RunLevel', namespaces=NS) == 'LeastPrivilege'
        assert task.findtext('t:Triggers/t:BootTrigger/t:Delay', namespaces=NS) == 'PT30S'
        assert task.findtext('t:Settings/t:MultipleInstancesPolicy', namespaces=NS) == 'IgnoreNew'
        assert task.findtext('t:Actions/t:Exec/t:WorkingDirectory', namespaces=NS) == str(root)
        if 'Watchdog' in plan.name:
            assert task.findtext('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval', namespaces=NS) == 'PT2M'


def test_s4u_conversion_preserves_existing_stored_password_configuration(tmp_path):
    process, output, _ = invoke(tmp_path, 'S4U', original='Password', apply=True)
    assert process.returncode != 0
    assert 'Cannot safely roll back stored Password credentials' in process.stderr
    assert len(list(output.glob('*.before.xml'))) == 2
    assert not list(output.glob('*.installed.xml'))
