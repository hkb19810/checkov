"""Actual Runner scans: ensure paths preserve module inputs, outputs and findings."""
import json
import os
from pathlib import Path

import pytest

from checkov.runner_filter import RunnerFilter
from checkov.terraform.runner import Runner

CHILD = '''
variable "enabled" { type = bool }
resource "aws_ebs_encryption_by_default" "test" {
  enabled = var.enabled
}
'''


def write_tf(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def run(root):
    report = Runner().run(root_folder=str(root), runner_filter=RunnerFilter(
        checks=['CKV_AWS_106'], download_external_modules=False, show_progress_bar=False))
    assert not report.parsing_errors
    rows = [{'resource': r.resource, 'result': r.check_result['result'].name,
             'file': r.file_path} for r in report.passed_checks + report.failed_checks]
    return rows


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('source_style', [
    'child_relative', 'parent_relative', 'relative_dot_segments',
    'absolute_windows', 'absolute_forward_slashes', 'root_forward_slashes',
    'child_relative_backslashes', 'parent_relative_backslashes',
])
def test_local_module_paths(tmp_path, source_style, enabled):
    if source_style.startswith('absolute_') and os.name != 'nt':
        pytest.skip('Windows drive-letter source paths require Windows')
    root = tmp_path / 'project with spaces' / 'root'
    child = root / 'modules' / 'child'
    source = './modules/child'
    if source_style == 'parent_relative':
        child = root.parent / 'shared' / 'child'
        source = '../shared/child'
    elif source_style == 'relative_dot_segments':
        source = './modules/../modules/child'
    elif source_style == 'absolute_windows':
        source = str(child)
    elif source_style == 'absolute_forward_slashes':
        source = child.as_posix()
    elif source_style == 'child_relative_backslashes':
        source = r'.\modules\child'
    elif source_style == 'parent_relative_backslashes':
        child = root.parent / 'shared' / 'child'
        source = r'..\shared\child'
    write_tf(child / 'main.tf', CHILD)
    write_tf(root / 'main.tf', f'''
module "consumer" {{
  source = {json.dumps(source)}
  enabled = {str(enabled).lower()}
}}
''')
    rows = run(root.as_posix() if source_style == 'root_forward_slashes' else root)
    assert len(rows) == 1
    assert rows[0]['resource'] == 'module.consumer.aws_ebs_encryption_by_default.test'
    assert rows[0]['result'] == ('PASSED' if enabled else 'FAILED')


@pytest.mark.parametrize('enabled', [True, False])
def test_sibling_output_to_nested_module(tmp_path, enabled):
    root = tmp_path / 'project with spaces' / 'root'
    write_tf(root / 'producer/main.tf', '''
variable "enabled" { type = bool }
output "enabled" { value = var.enabled }
''')
    write_tf(root / 'consumer/main.tf', '''
variable "enabled" { type = bool }
module "leaf" {
  source = "../../shared/leaf"
  enabled = var.enabled
}
''')
    write_tf(root.parent / 'shared/leaf/main.tf', CHILD)
    write_tf(root / 'main.tf', f'''
module "producer" {{
  source = "./producer"
  enabled = {str(enabled).lower()}
}}
module "consumer" {{
  source = "./consumer"
  enabled = module.producer.enabled
}}
''')
    rows = run(root)
    assert len(rows) == 1
    assert rows[0]['resource'] == 'module.consumer.module.leaf.aws_ebs_encryption_by_default.test'
    assert rows[0]['result'] == ('PASSED' if enabled else 'FAILED')


def test_shared_module_different_inputs_and_paths(tmp_path):
    root = tmp_path / 'project with spaces' / 'root'
    write_tf(root / 'modules/child/main.tf', CHILD)
    write_tf(root / 'main.tf', '''
module "secure" {
  source = "./modules/child"
  enabled = true
}
module "insecure" {
  source = "./modules/../modules/child"
  enabled = false
}
''')
    rows = run(root)
    assert {r['resource']: r['result'] for r in rows} == {
        'module.secure.aws_ebs_encryption_by_default.test': 'PASSED',
        'module.insecure.aws_ebs_encryption_by_default.test': 'FAILED',
    }
