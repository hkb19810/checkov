import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch
from checkov.common.bridgecrew.severities import Severities, BcSeverities
from checkov.common.models.enums import CheckResult
from checkov.common.output.record import Record
from checkov.common.output.report import CheckType, Report
from checkov.runner_filter import RunnerFilter
from checkov.helm.runner import Runner, fix_report_paths, _get_chart_remote_repos, _get_blocked_helm_repos, \
    _repo_matches_prefix
from tests.helm.utils import helm_exists


class TestRunnerValid(unittest.TestCase):
    @unittest.skipIf(not helm_exists(), "helm not installed")
    def test_record_relative_path_with_relative_dir(self):
        # test whether the record's repo_file_path is correct, relative to the CWD (with a / at the start).

        # this is just constructing the scan dir as normal
        current_dir = os.path.dirname(os.path.realpath(__file__))
        scan_dir_path = os.path.join(current_dir, "runner", "resources", "infrastructure")

        # this is the relative path to the directory to scan (what would actually get passed to the -d arg)
        dir_rel_path = os.path.relpath(scan_dir_path).replace("\\", "/")

        checks_allowlist = ["CKV_K8S_42"]

        runner = Runner()
        report = runner.run(
            root_folder=dir_rel_path, runner_filter=RunnerFilter(framework=["helm"], checks=checks_allowlist)
        )

        all_checks = report.failed_checks + report.passed_checks
        self.assertEqual(len(report.passed_checks), 0)
        self.assertEqual(len(report.failed_checks), 1)
        self.assertEqual(report.check_type, CheckType.HELM)
        for record in all_checks:
            self.assertIn(record.repo_file_path, record.file_path)
        for resource in report.resources:
            self.assertIn('/helm-tiller/pwnchart/templates', resource)

    @unittest.skipIf(not helm_exists(), "helm not installed")
    def test_runner_honors_enforcement_rules(self):
        current_dir = os.path.dirname(os.path.realpath(__file__))
        scan_dir_path = os.path.join(current_dir, "runner", "resources", "infrastructure")

        runner = Runner()
        filter = RunnerFilter(framework=['helm'], use_enforcement_rules=True)
        # this is not quite a true test, because the checks don't have severities. However, this shows that the check registry
        # passes the report type properly to RunnerFilter.should_run_check, and we have tests for that method
        filter.enforcement_rule_configs = {CheckType.HELM: Severities[BcSeverities.OFF]}
        report = runner.run(
            root_folder=scan_dir_path, runner_filter=filter
        )

        self.assertEqual(len(report.failed_checks), 0)
        self.assertEqual(len(report.passed_checks), 0)
        self.assertEqual(len(report.skipped_checks), 0)
        self.assertEqual(len(report.parsing_errors), 0)

    @unittest.skipIf(not helm_exists(), "helm not installed")
    def test_scan_vendored_chart_with_helm_warnings(self):
        chart_dir = Path(__file__).parent / 'runner/resources/schema-registry'
        report = Runner().run(str(chart_dir), runner_filter=RunnerFilter(framework=['helm']))

        self.assertTrue(report.resources)
        self.assertEqual(report.parsing_errors, [])

    @unittest.skipIf(not helm_exists(), "helm not installed")
    def test_get_binary_output_from_directory_equals_to_get_binary_result(self):
        current_dir = os.path.dirname(os.path.realpath(__file__))
        scan_dir_path = os.path.join(current_dir, "runner", "resources", "image_referencer")

        runner_filter = RunnerFilter(framework=['helm'], use_enforcement_rules=False)

        chart_item = Runner.parse_helm_chart_details(scan_dir_path)
        regular_result = Runner.get_binary_output(chart_item, target_dir='./tmp', helm_command="helm",
                                                  runner_filter=runner_filter)
        result_from_directory = Runner.get_binary_output_from_directory(str(scan_dir_path),
                                                                        target_dir='./tmp', helm_command="helm",
                                                                        runner_filter=runner_filter)
        assert regular_result == result_from_directory

    def test_fix_report_paths(self):
        # Create a test report with some checks
        report = Report(CheckType.HELM)
        tmp_dir = "/tmp/helm_test"
        original_root_folder = "/original/root"

        # Create template mapping
        template_mapping = {
            "/tmp/helm_test/manifest1.yaml": "/original/root/chart/templates/manifest1.yaml",
            "/tmp/helm_test/manifest2.yaml": "/original/root/chart/templates/manifest2.yaml",
            "/tmp/helm_test/unknown.yaml": "/original/root/chart/templates/unknown.yaml",
        }

        # Create some test records
        failed_check1 = Record(
            check_id="CKV_K8S_1",
            check_name="Test check 1",
            check_result={"result": CheckResult.FAILED},
            code_block=[],
            file_path=f"{tmp_dir}/manifest1.yaml",
            file_line_range=[1, 10],
            resource="resource1",
            evaluations={},
            check_class="",
            file_abs_path=f"{tmp_dir}/manifest1.yaml",
            entity_tags={},
        )

        passed_check1 = Record(
            check_id="CKV_K8S_2",
            check_name="Test check 2",
            check_result={"result": CheckResult.PASSED},
            code_block=[],
            file_path=f"{tmp_dir}/manifest2.yaml",
            file_line_range=[1, 10],
            resource="resource2",
            evaluations={},
            check_class="",
            file_abs_path=f"{tmp_dir}/manifest2.yaml",
            entity_tags={},
        )

        # Add unknown path check to test edge case
        unknown_check = Record(
            check_id="CKV_K8S_3",
            check_name="Test check 3",
            check_result={"result": CheckResult.FAILED},
            code_block=[],
            file_path=f"{tmp_dir}/unknown.yaml",
            file_line_range=[1, 10],
            resource="resource3",
            evaluations={},
            check_class="",
            file_abs_path=f"{tmp_dir}/unknown.yaml",
            entity_tags={},
        )

        report.failed_checks = [failed_check1, unknown_check]
        report.passed_checks = [passed_check1]

        # Add resources to report
        report.resources = {
            f"{tmp_dir}/manifest1.yaml:resource1",
            f"{tmp_dir}/manifest2.yaml:resource2",
            f"{tmp_dir}/unknown.yaml:resource3"
        }

        # Run the function to test
        fix_report_paths(report, tmp_dir, template_mapping, original_root_folder)

        # Check the results
        self.assertEqual(failed_check1.repo_file_path, "/chart/templates/manifest1.yaml")
        self.assertEqual(failed_check1.file_path, "/chart/templates/manifest1.yaml")
        self.assertEqual(failed_check1.file_abs_path, "/original/root/chart/templates/manifest1.yaml")

        self.assertEqual(passed_check1.repo_file_path, "/chart/templates/manifest2.yaml")
        self.assertEqual(passed_check1.file_path, "/chart/templates/manifest2.yaml")
        self.assertEqual(passed_check1.file_abs_path, "/original/root/chart/templates/manifest2.yaml")

        # Unknown path should just have the temp dir prefix removed
        self.assertEqual(unknown_check.repo_file_path, "/chart/templates/unknown.yaml")

        # Check that resources are also updated
        self.assertIn("/original/root/chart/templates/manifest1.yaml:resource1", report.resources)
        self.assertIn("/original/root/chart/templates/manifest2.yaml:resource2", report.resources)
        self.assertIn("/original/root/chart/templates/unknown.yaml:resource3", report.resources)

    def test_parse_output(self):
        # Create a temp directory for the test
        with tempfile.TemporaryDirectory() as target_dir:
            # Sample helm template output with multiple resources
            helm_output = b"---\n# Source: mychart/templates/service.yaml\napiVersion: v1\nkind: Service\nmetadata:\n  name: example-service\nspec:\n  selector:\n    app: example\n  ports:\n    - port: 80\n      targetPort: 8080\n---\n# Source: mychart/templates/deployment.yaml\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: example-deployment\nspec:\n  replicas: 3\n  template:\n    metadata:\n      labels:\n        app: example\n    spec:\n      containers:\n      - name: example\n        image: example:1.0"

            # Create a temporary chart directory
            with tempfile.TemporaryDirectory() as chart_dir:
                # Set up the chart directory structure
                templates_dir = os.path.join(chart_dir, "templates")
                os.makedirs(templates_dir, exist_ok=True)

                # Create template files to test mapping
                with open(os.path.join(templates_dir, "service.yaml"), 'w') as f:
                    f.write("# Original service template")

                with open(os.path.join(templates_dir, "deployment.yaml"), 'w') as f:
                    f.write("# Original deployment template")

                # Create an empty template mapping dictionary
                template_mapping = {}

                # Call the parse_output function
                Runner._parse_output(target_dir, helm_output, chart_dir, template_mapping)

                # Check template mapping was populated correctly
                expected_mapping = {
                    f'{target_dir}/mychart/templates/service.yaml': os.path.join(chart_dir, "templates/service.yaml"),
                    f'{target_dir}/mychart/templates/deployment.yaml': os.path.join(chart_dir,
                                                                                    "templates/deployment.yaml")
                }

                # Compare the mappings - normalize paths for comparison
                normalized_template_mapping = {k.replace('\\', '/'): v.replace('\\', '/')
                                               for k, v in template_mapping.items()}
                normalized_expected_mapping = {k.replace('\\', '/'): v.replace('\\', '/')
                                               for k, v in expected_mapping.items()}

                self.assertEqual(normalized_template_mapping, normalized_expected_mapping)

                # Verify file content was written correctly
                service_file_path = os.path.join(target_dir, "mychart/templates/service.yaml")
                deployment_file_path = os.path.join(target_dir, "mychart/templates/deployment.yaml")

                if os.path.exists(service_file_path):
                    with open(service_file_path, 'r') as f:
                        content = f.read()
                        self.assertIn("kind: Service", content)
                        self.assertIn("name: example-service", content)

                if os.path.exists(deployment_file_path):
                    with open(deployment_file_path, 'r') as f:
                        content = f.read()
                        self.assertIn("kind: Deployment", content)
                        self.assertIn("name: example-deployment", content)

    @unittest.skipIf(not helm_exists(), "helm not installed")
    def test_check_system_deps(self):
        o = Runner().check_system_deps()
        assert o == None
        r = Runner()
        r.helm_command = 'thisshouldfail'
        assert r.check_system_deps() == "helm"


class TestHelmDependencyRemoteRepos(unittest.TestCase):
    """Tests for helm remote dependency repository allowlist.

    Default behaviour: helm template renders only what is bundled, without --dependency-update.
    Downloads are opt-in via CHECKOV_HELM_DEPENDENCY_UPDATE=true. Then blocking is opt-in via
    CHECKOV_HELM_ALLOWED_REMOTE_REPOS — an allowlist of trusted repo URL prefixes.
    Dependency repos NOT in the allowlist cause the build to be skipped.
    """

    _REMOTE_DEP_DIR = str(Path(__file__).parent / "runner/resources/example_remote_dep")

    def _make_chart_item(self) -> tuple[str, dict]:
        return (self._REMOTE_DEP_DIR, {"name": "example-chart", "version": "0.1.0"})

    # ------------------------------------------------------------------
    # _get_chart_remote_repos() unit tests
    # ------------------------------------------------------------------

    def test_get_chart_remote_repos_detects_http_url(self):
        """_get_chart_remote_repos() must return http:// dependency repos."""
        repos = _get_chart_remote_repos(self._REMOTE_DEP_DIR)
        self.assertIn("http://192.0.2.1", repos)

    def test_get_chart_remote_repos_returns_empty_for_missing_dir(self):
        """_get_chart_remote_repos() must not raise for a missing directory."""
        self.assertEqual(_get_chart_remote_repos("/nonexistent/path"), [])

    def test_get_chart_remote_repos_detects_oci_and_requirements_yaml(self):
        with tempfile.TemporaryDirectory() as chart_dir:
            Path(chart_dir, "Chart.yaml").write_text(
                "apiVersion: v2\nname: app\nversion: 1.0.0\ndependencies:\n"
                "- name: private\n  version: 1.0.0\n  repository: oci://registry.example.com/charts\n"
                "- name: local\n  version: 1.0.0\n  repository: file://../local\n"
            )
            Path(chart_dir, "requirements.yaml").write_text(
                "dependencies:\n- name: legacy\n  version: 1.0.0\n  repository: https://charts.example.com\n"
            )
            repos = _get_chart_remote_repos(chart_dir)
        self.assertEqual(repos, ["oci://registry.example.com/charts", "https://charts.example.com"])

    # ------------------------------------------------------------------
    # _get_blocked_helm_repos() unit tests (allowlist logic)
    # ------------------------------------------------------------------

    def test_get_blocked_helm_repos_returns_empty_when_no_env_var(self):
        """Default: no env var → _get_blocked_helm_repos() returns [] (allow all)."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHECKOV_HELM_ALLOWED_REMOTE_REPOS", None)
            repos = _get_blocked_helm_repos(self._REMOTE_DEP_DIR)
        self.assertEqual(repos, [])

    def test_get_blocked_helm_repos_returns_blocked_when_not_in_allowlist(self):
        """Repos NOT in the allowlist must be returned (they will be blocked)."""
        env = {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "https://charts.trusted.org/"}
        with mock.patch.dict(os.environ, env):
            repos = _get_blocked_helm_repos(self._REMOTE_DEP_DIR)
        self.assertIn("http://192.0.2.1", repos)

    def test_get_blocked_helm_repos_returns_empty_when_all_in_allowlist(self):
        """When all repos are in the allowlist, nothing is blocked."""
        env = {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "http://192.0.2.1"}
        with mock.patch.dict(os.environ, env):
            repos = _get_blocked_helm_repos(self._REMOTE_DEP_DIR)
        self.assertEqual(repos, [])

    def test_get_blocked_helm_repos_wildcard_allows_all(self):
        """'*' allows all repos."""
        env = {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "https://charts.trusted.org/, *,"}
        with mock.patch.dict(os.environ, env):
            repos = _get_blocked_helm_repos(self._REMOTE_DEP_DIR)
        self.assertEqual(repos, [])

    # ------------------------------------------------------------------
    # allowlist host matching (scheme, host and port must match exactly)
    # ------------------------------------------------------------------

    def _blocked(self, allowlist: str, repos: list[str]) -> list[str]:
        env = {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": allowlist}
        with mock.patch.dict(os.environ, env), \
                mock.patch("checkov.helm.runner._get_chart_remote_repos", return_value=repos):
            return _get_blocked_helm_repos(self._REMOTE_DEP_DIR)

    def _assert_allowed(self, allowlist: str, repo: str) -> None:
        self.assertEqual(self._blocked(allowlist, [repo]), [], f"{repo} should be allowed by {allowlist}")

    def _assert_blocked(self, allowlist: str, repo: str) -> None:
        self.assertEqual(self._blocked(allowlist, [repo]), [repo], f"{repo} should be blocked by {allowlist}")

    def test_allowlist_lookalike_host_not_allowed(self):
        self._assert_blocked("https://charts.example.com", "https://charts.example.com.other.net/x")

    def test_allowlist_port_must_match_exactly(self):
        self._assert_blocked("https://192.0.2.1:844", "https://192.0.2.1:8443/x")
        self._assert_allowed("https://192.0.2.1:8443/", "https://192.0.2.1:8443/x")

    def test_allowlist_default_port_matches(self):
        self._assert_allowed("https://charts.example.com", "https://charts.example.com:443/x")
        self._assert_allowed("http://192.0.2.1", "http://192.0.2.1:80/x")
        self._assert_blocked("https://charts.example.com", "https://charts.example.com:80/x")

    def test_allowlist_userinfo_ignored_for_host_match(self):
        self._assert_allowed("https://charts.example.com", "https://u:p@charts.example.com/x")
        self._assert_blocked("https://charts.example.com", "https://charts.example.com@other.net/x")

    def test_allowlist_query_and_fragment_not_used_as_host(self):
        self._assert_blocked("https://charts.example.com", "https://other.net/x?@charts.example.com/")
        self._assert_blocked("https://charts.example.com", "https://other.net/x#@charts.example.com/")

    def test_allowlist_scheme_must_match(self):
        self._assert_blocked("https://charts.example.com", "http://charts.example.com/x")

    def test_allowlist_host_match_is_case_insensitive(self):
        self._assert_allowed("https://Charts.Example.com/", "HTTPS://CHARTS.EXAMPLE.COM/x")

    def test_allowlist_path_prefix(self):
        self._assert_allowed("https://charts.example.com/stable", "https://charts.example.com/stable/app")
        self._assert_blocked("https://charts.example.com/stable", "https://charts.example.com/incubator/app")

    def test_allowlist_invalid_port_not_allowed(self):
        self._assert_blocked("https://charts.example.com", "https://charts.example.com:99999/x")
        self._assert_blocked("https://charts.example.com", "https://charts.example.com:abc/x")
        self._assert_blocked("https://charts.example.com:abc", "https://charts.example.com/x")

    def test_allowlist_multiple_prefixes(self):
        repos = ["https://charts.example.com/x", "https://charts.example.com.other.net/x", "http://192.0.2.1/y"]
        self.assertEqual(
            self._blocked("https://charts.example.com, http://192.0.2.1", repos),
            ["https://charts.example.com.other.net/x"],
        )

    def test_repo_matches_prefix_oci(self):
        self.assertTrue(_repo_matches_prefix("oci://registry.example.com/charts/app", "oci://registry.example.com/charts"))
        self.assertFalse(
            _repo_matches_prefix("oci://registry.example.com.other.net/charts/app", "oci://registry.example.com/charts")
        )
        self.assertFalse(_repo_matches_prefix("oci://registry.example.com:5000/charts/app", "oci://registry.example.com/charts"))

    def test_repo_matches_prefix_non_url_forms_unchanged(self):
        # Values without '://' keep the plain prefix comparison.
        self.assertTrue(_repo_matches_prefix("@bitnami", "@bit"))
        self.assertTrue(_repo_matches_prefix("alias:bitnami", "alias:"))
        self.assertFalse(_repo_matches_prefix("@bitnami", "alias:"))
        self.assertTrue(_repo_matches_prefix("https://charts.example.com/x", "https"))
        self.assertFalse(_repo_matches_prefix("../local-chart", "https://charts.example.com"))

    # ------------------------------------------------------------------
    # get_binary_output() integration tests
    # ------------------------------------------------------------------

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "none", "CHECKOV_HELM_DEPENDENCY_UPDATE": ""})
    def test_renders_bundled_dependencies_without_update_or_allowlist_by_default(self):
        output = b"---\n# Source: example/templates/pod.yaml\nkind: Pod\n"
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, b"")) as run:
            result, _ = Runner.get_binary_output(self._make_chart_item(), "./tmp", "helm", RunnerFilter())

        self.assertEqual(result, output)
        self.assertEqual(run.call_args.args[0], ["helm", "template", self._REMOTE_DEP_DIR])

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "", "CHECKOV_HELM_DEPENDENCY_UPDATE": "true"})
    def test_successful_stdout_is_used_even_with_oci_progress_on_stderr(self):
        chart_item = self._make_chart_item()
        output = b"---\n# Source: example/templates/pod.yaml\nkind: Pod\n"
        completed = subprocess.CompletedProcess([], 0, output, b"Pulled: registry/chart:1.0.0\nDigest: sha256:abc\n")
        with mock.patch("subprocess.run", return_value=completed) as run:
            result, _ = Runner.get_binary_output(chart_item, "./tmp", "helm", RunnerFilter())

        self.assertEqual(result, output)
        self.assertEqual(run.call_args.args[0], ["helm", "template", "--dependency-update", self._REMOTE_DEP_DIR])

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": ""})
    def test_nonzero_helm_exit_discards_stdout_even_without_stderr(self):
        chart_item = self._make_chart_item()
        completed = subprocess.CompletedProcess([], 1, b"partial YAML output", b"")
        with mock.patch("subprocess.run", return_value=completed):
            result, _ = Runner.get_binary_output(chart_item, "./tmp", "helm", RunnerFilter())

        self.assertIsNone(result)

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "https://charts.trusted.org/",
                                  "CHECKOV_HELM_DEPENDENCY_UPDATE": "true"})
    def test_build_skipped_when_repo_not_in_allowlist(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            result, _ = Runner.get_binary_output(self._make_chart_item(), "./tmp", "helm", RunnerFilter())

        self.assertIsNone(result)
        self.assertFalse(any(call.args[0][1] == "template" for call in run.call_args_list))

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "http://192.0.2.1",
                                  "CHECKOV_HELM_DEPENDENCY_UPDATE": "true"})
    def test_build_proceeds_when_all_repos_in_allowlist(self):
        output = b"apiVersion: v1\nkind: ConfigMap\n"
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, b"")):
            result, _ = Runner.get_binary_output(self._make_chart_item(), "./tmp", "helm", RunnerFilter())
        self.assertEqual(result, output)

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "none", "CHECKOV_HELM_DEPENDENCY_UPDATE": "true"})
    def test_oci_dependency_blocked_when_downloads_enabled(self):
        with tempfile.TemporaryDirectory() as chart_dir:
            Path(chart_dir, "Chart.yaml").write_text(
                "apiVersion: v2\nname: app\nversion: 1.0.0\ndependencies:\n"
                "- name: private\n  version: 1.0.0\n  repository: oci://registry.example.com/charts\n"
            )
            with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
                result, _ = Runner.get_binary_output((chart_dir, {"name": "app"}), "./tmp", "helm", RunnerFilter())

        self.assertIsNone(result)
        self.assertFalse(any(call.args[0][1] == "template" for call in run.call_args_list))


@unittest.skipIf(not helm_exists(), "helm not installed")
class TestIncompleteHelmChart(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.parent = self.root / "parent"
        self.parent.mkdir()
        self.chart_yaml = self.parent / "Chart.yaml"
        self.chart_yaml.write_text(
            "apiVersion: v2\nname: parent\nversion: 1.0.0\ndependencies:\n"
            "- name: unavailable\n  version: 1.0.0\n  repository: file://../unavailable\n"
        )

    def test_cli_exits_nonzero_when_chart_has_no_scannable_resources(self):
        project = Path(__file__).resolve().parents[2]
        config = self.root / "config.yaml"
        config.write_text("{}\n")
        env = dict(os.environ, PYTHONPATH=str(project), BC_SKIP_MAPPING="true",
                   CHECKOV_HELM_ALLOWED_REMOTE_REPOS="")
        env.pop("CKV_PARSE_ERROR_FAIL", None)
        result = subprocess.run(
            [sys.executable, str(project / "bin/checkov"), "-d", str(self.parent), "--framework", "helm",
             "--config-file", str(config), "--skip-download", "--quiet", "-o", "json"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=90, check=False,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertGreater(report["summary"]["parsing_errors"], 0)
        self.assertIn("Chart.yaml", report["results"]["parsing_errors"][0])

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "none", "CHECKOV_HELM_DEPENDENCY_UPDATE": "true"})
    def test_blocked_dependency_is_reported_without_rendering_or_partial_recovery(self):
        metadata = self.chart_yaml.read_text().replace("file://../unavailable", "https://charts.example.com")
        self.chart_yaml.write_text(metadata)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            report = Runner().run(str(self.parent), runner_filter=RunnerFilter(framework=["helm"]))

        self.assertTrue(any("allowlist" in error for error in report.parsing_errors))
        self.assertFalse(any(call.args[0][1] == "template" for call in run.call_args_list))

    def test_missing_dependency_preserves_findings_from_packaged_sibling(self):
        sibling = self.root / "available"
        templates = sibling / "templates"
        templates.mkdir(parents=True)
        (sibling / "Chart.yaml").write_text("apiVersion: v2\nname: available\nversion: 1.0.0\n")
        (sibling / "values.yaml").write_text("image: example:1.0.0\n")
        (templates / "pod.yaml").write_text(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: available-sibling\n"
            "spec:\n  containers:\n    - name: app\n      image: {{ .Values.image | quote }}\n"
        )
        overrides = self.root / "overrides.yaml"
        overrides.write_text("available:\n  image: example:latest\n")
        charts = self.parent / "charts"
        charts.mkdir()
        subprocess.run(["helm", "package", str(sibling), "--destination", str(charts)],
                       check=True, capture_output=True, timeout=30)
        chart_yaml = self.chart_yaml.read_text() + "- name: available\n  version: 1.0.0\n  repository: file://../available\n"
        self.chart_yaml.write_text(chart_yaml)

        runner_filter = RunnerFilter(framework=["helm"], checks=["CKV_K8S_14"], var_files=[str(overrides)])
        report = Runner().run(str(self.parent), runner_filter=runner_filter)

        self.assertTrue(any("unavailable" in error for error in report.parsing_errors))
        findings = [record for record in report.failed_checks if "available-sibling" in record.resource]
        self.assertEqual([record.check_id for record in findings], ["CKV_K8S_14"])
        self.assertTrue(any("partial" in detail and "unavailable" in detail for detail in findings[0].details))
        self.assertEqual(self.chart_yaml.read_text(), chart_yaml)

    def _add_unbundled_file_dependency(self) -> None:
        # Keep the dependency source outside Checkov's chart discovery so its
        # finding can only come from rendering it in the parent release.
        available = self.parent / ".sources" / "available"
        available.mkdir(parents=True)
        (available / "Chart.yaml").write_text("apiVersion: v2\nname: available\nversion: 1.0.0\n")
        with self.chart_yaml.open("a") as f:
            f.write("- name: available\n  version: 1.0.0\n  repository: file://.sources/available\n")
        for directory in (self.parent, available):
            templates = directory / "templates"
            templates.mkdir()
            (templates / "pod.yaml").write_text(
                "apiVersion: v1\nkind: Pod\nmetadata:\n  name: {{ .Chart.Name }}\n"
                "spec:\n  containers:\n    - name: app\n      image: example:latest\n"
            )
        self.assertFalse((self.parent / "charts").exists())

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_DEPENDENCY_UPDATE": "true"})
    def test_retry_builds_missing_but_resolvable_sibling(self):
        self._add_unbundled_file_dependency()

        report = Runner().run(str(self.parent), runner_filter=RunnerFilter(framework=["helm"], checks=["CKV_K8S_14"]))

        self.assertEqual(len(report.parsing_errors), 1)
        self.assertIn("unavailable", report.parsing_errors[0])
        self.assertCountEqual([record.resource for record in report.failed_checks],
                              ["Pod.default.parent", "Pod.default.available"])
        for record in report.failed_checks:
            self.assertTrue(any("partial" in detail and "unavailable" in detail for detail in record.details))

    @mock.patch.dict(os.environ, {"CHECKOV_HELM_DEPENDENCY_UPDATE": ""})
    def test_unbundled_dependencies_are_omitted_when_downloads_are_off(self):
        self._add_unbundled_file_dependency()

        report = Runner().run(str(self.parent), runner_filter=RunnerFilter(framework=["helm"], checks=["CKV_K8S_14"]))

        self.assertEqual(len(report.parsing_errors), 1)
        self.assertIn("omitted dependencies: available, unavailable", report.parsing_errors[0])
        self.assertEqual([record.resource for record in report.failed_checks], ["Pod.default.parent"])
        self.assertFalse((self.parent / "charts").exists())


@unittest.skipIf(not helm_exists(), "helm not installed")
@mock.patch.dict(os.environ, {"CHECKOV_HELM_ALLOWED_REMOTE_REPOS": "none", "CHECKOV_HELM_DEPENDENCY_UPDATE": ""})
class TestBundledHelmDependencies(unittest.TestCase):
    """Charts render offline from their bundled dependencies, whatever repository the declarations name."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sources = self.root / ".sources"

    def _chart(self, directory: Path, name: str, dependencies: tuple[tuple[str, str], ...] = (),
               api_version: str = "v2") -> Path:
        (directory / "templates").mkdir(parents=True)
        declarations = "".join(f"- name: {dep}\n  version: 1.0.0\n  repository: {repo}\n" for dep, repo in dependencies)
        metadata = f"apiVersion: {api_version}\nname: {name}\nversion: 1.0.0\n"
        if declarations and api_version == "v1":
            (directory / "requirements.yaml").write_text("dependencies:\n" + declarations)
        elif declarations:
            metadata += "dependencies:\n" + declarations
        (directory / "Chart.yaml").write_text(metadata)
        (directory / "templates" / "pod.yaml").write_text(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: {{ .Chart.Name }}\n"
            "spec:\n  containers:\n    - name: app\n      image: example:latest\n"
        )
        return directory

    def _bundle(self, chart: Path, into: Path) -> None:
        (into / "charts").mkdir(exist_ok=True)
        subprocess.run(["helm", "package", str(chart), "--destination", str(into / "charts")],
                       check=True, capture_output=True, timeout=30)

    def _scan(self, chart: Path) -> Report:
        return Runner().run(str(chart), runner_filter=RunnerFilter(framework=["helm"], checks=["CKV_K8S_14"]))

    def test_bundled_public_and_private_dependencies_are_scanned_without_a_registry(self):
        parent = self._chart(self.root / "app", "app", (("public", "https://charts.example.com"),
                                                        ("private", "oci://registry.example.com/charts")))
        self._bundle(self._chart(self.sources / "public", "public"), parent)
        self._bundle(self._chart(self.sources / "private", "private"), parent)

        report = self._scan(parent)

        self.assertEqual(report.parsing_errors, [])
        self.assertCountEqual([record.resource for record in report.failed_checks],
                              ["Pod.default.app", "Pod.default.public", "Pod.default.private"])

    def test_missing_private_dependency_is_reported_and_bundled_public_is_scanned(self):
        parent = self._chart(self.root / "app", "app", (("public", "https://charts.example.com"),
                                                        ("private", "oci://registry.example.com/charts")))
        self._bundle(self._chart(self.sources / "public", "public"), parent)

        report = self._scan(parent)

        self.assertEqual(len(report.parsing_errors), 1)
        self.assertIn("omitted dependencies: private", report.parsing_errors[0])
        self.assertCountEqual([record.resource for record in report.failed_checks],
                              ["Pod.default.app", "Pod.default.public"])

    def test_v1_chart_uses_bundled_requirements_dependency(self):
        parent = self._chart(self.root / "legacy", "legacy", (("public", "https://charts.example.com"),), api_version="v1")
        self._bundle(self._chart(self.sources / "public", "public"), parent)

        report = self._scan(parent)

        self.assertEqual(report.parsing_errors, [])
        self.assertCountEqual([record.resource for record in report.failed_checks],
                              ["Pod.default.legacy", "Pod.default.public"])

    def test_library_chart_is_rendered_only_through_the_chart_that_uses_it(self):
        common = self.root / "charts" / "common"
        (common / "templates").mkdir(parents=True)
        (common / "Chart.yaml").write_text("apiVersion: v2\nname: common\nversion: 1.0.0\ntype: library\n")
        (common / "templates" / "_helpers.tpl").write_text('{{- define "common.image" -}}example:latest{{- end -}}\n')
        app = self.root / "charts" / "app"
        (app / "templates").mkdir(parents=True)
        (app / "Chart.yaml").write_text(
            "apiVersion: v2\nname: app\nversion: 1.0.0\ndependencies:\n"
            "- name: common\n  version: 1.0.0\n  repository: file://../common\n"
        )
        (app / "templates" / "pod.yaml").write_text(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: app\n"
            "spec:\n  containers:\n    - name: app\n      image: {{ include \"common.image\" . }}\n"
        )
        self._bundle(common, app)

        report = self._scan(self.root)

        self.assertEqual(report.parsing_errors, [])
        self.assertEqual([record.resource for record in report.failed_checks], ["Pod.default.app"])

    def test_nested_bundled_dependency_is_scanned(self):
        child = self._chart(self.sources / "child", "child", (("grandchild", "oci://registry.example.com/charts"),))
        self._bundle(self._chart(self.sources / "grandchild", "grandchild"), child)
        parent = self._chart(self.root / "app", "app", (("child", "https://charts.example.com"),))
        self._bundle(child, parent)

        report = self._scan(parent)

        self.assertEqual(report.parsing_errors, [])
        self.assertCountEqual([record.resource for record in report.failed_checks],
                              ["Pod.default.app", "Pod.default.child", "Pod.default.grandchild"])


class TestRenderedSourcePaths(unittest.TestCase):
    """The '# Source: ' lines of the helm template output are used as file paths - they must stay in the target dir"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.target_dir = os.path.join(self.root, "target")
        self.outside_dir = os.path.join(self.root, "outside")
        self.chart_dir = os.path.join(self.root, "chart")
        for d in (self.target_dir, self.outside_dir, os.path.join(self.chart_dir, "templates")):
            os.makedirs(d)
        with open(os.path.join(self.chart_dir, "templates", "cm.yaml"), "w") as f:
            f.write("# template")

    def tearDown(self):
        self._tmp.cleanup()

    def _parse(self, output: str) -> dict:
        template_mapping = {}
        Runner._parse_output(self.target_dir, output.encode(), self.chart_dir, template_mapping)
        return template_mapping

    def _written_files(self) -> list:
        return sorted(
            os.path.relpath(os.path.join(dirpath, name), self.root)
            for dirpath, _, filenames in os.walk(self.root)
            for name in filenames
            if not os.path.join(dirpath, name).startswith(self.chart_dir + os.sep)
        )

    def _read(self, relative_path: str) -> str:
        with open(os.path.join(self.target_dir, relative_path)) as f:
            return f.read()

    def test_valid_sources_written_and_mapped_as_before(self):
        mapping = self._parse(
            "---\n# Source: mychart/templates/cm.yaml\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n"
            "---\n# Source: mychart/templates/sub/dir/other.yaml\napiVersion: v1\nkind: Secret\n"
        )
        self.assertEqual(
            self._written_files(),
            [os.path.join("target", "mychart", "templates", "cm.yaml"),
             os.path.join("target", "mychart", "templates", "sub", "dir", "other.yaml")],
        )
        self.assertEqual(
            mapping,
            {f"{self.target_dir}/mychart/templates/cm.yaml": os.path.join(self.chart_dir, "templates/cm.yaml")},
        )
        self.assertIn("kind: ConfigMap", self._read("mychart/templates/cm.yaml"))

    def test_valid_source_with_dots_in_names(self):
        self._parse("---\n# Source: my.chart/templates/..hidden..yaml\napiVersion: v1\nkind: ConfigMap\n")
        self.assertEqual(self._written_files(), [os.path.join("target", "my.chart", "templates", "..hidden..yaml")])

    def test_valid_subchart_source(self):
        self._parse("---\n# Source: parent/charts/child/templates/cm.yaml\napiVersion: v1\nkind: ConfigMap\n")
        self.assertEqual(
            self._written_files(), [os.path.join("target", "parent", "charts", "child", "templates", "cm.yaml")]
        )

    def test_relative_traversal_source_is_not_written(self):
        mapping = self._parse(
            "---\n# Source: mychart/templates/cm.yaml\napiVersion: v1\nkind: ConfigMap\n"
            "---\n# Source: ../outside/.checkov.yml\nexternal-checks-dir: x\n"
        )
        self.assertEqual(os.listdir(self.outside_dir), [])
        self.assertEqual(self._written_files(), [os.path.join("target", "mychart", "templates", "cm.yaml")])
        self.assertNotIn("external-checks-dir", self._read("mychart/templates/cm.yaml"))
        self.assertTrue(all(k.startswith(self.target_dir + "/") for k in mapping))

    def test_absolute_source_is_not_written(self):
        self._parse(f"---\n# Source: {self.outside_dir}/file.yaml\nkey: value\n")
        self.assertEqual(os.listdir(self.outside_dir), [])
        self.assertEqual(self._written_files(), [])

    def test_nested_traversal_source_is_not_written(self):
        self._parse("---\n# Source: mychart/templates/../../../outside/file.yaml\nkey: value\n")
        self.assertEqual(os.listdir(self.outside_dir), [])
        self.assertEqual(self._written_files(), [])

    def test_backslash_traversal_source_is_not_written(self):
        self._parse("---\n# Source: ..\\outside\\file.yaml\nkey: value\n")
        self.assertEqual(os.listdir(self.outside_dir), [])
        self.assertEqual(self._written_files(), [])

    def test_document_after_skipped_source_is_written(self):
        self._parse(
            "---\n# Source: ../outside/file.yaml\nkey: value\n"
            "---\n# Source: mychart/templates/cm.yaml\napiVersion: v1\nkind: ConfigMap\n"
        )
        self.assertEqual(os.listdir(self.outside_dir), [])
        self.assertEqual(self._written_files(), [os.path.join("target", "mychart", "templates", "cm.yaml")])
        content = self._read("mychart/templates/cm.yaml")
        self.assertIn("kind: ConfigMap", content)
        self.assertNotIn("key: value", content)

    def test_same_valid_source_repeated_is_appended(self):
        self._parse(
            "---\n# Source: mychart/templates/cm.yaml\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n"
            "---\n# Source: ../outside/file.yaml\nkey: value\n"
            "---\n# Source: mychart/templates/cm.yaml\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n"
        )
        content = self._read("mychart/templates/cm.yaml")
        self.assertIn("name: a", content)
        self.assertIn("name: b", content)
        self.assertNotIn("key: value", content)

    def test_is_safe_source_path(self):
        cases = {
            "mychart/templates/cm.yaml": True,
            "my.chart/templates/a..b.yaml": True,
            "parent/charts/child/templates/cm.yaml": True,
            "cm.yaml": True,
            "": False,
            "..": False,
            "../cm.yaml": False,
            "mychart/../../cm.yaml": False,
            "/abs/cm.yaml": False,
            "..\\cm.yaml": False,
            "\\abs\\cm.yaml": False,
            "a\0b": False,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(Runner._is_safe_source_path(self.target_dir, source), expected)

    @unittest.skipIf(not helm_exists(), "helm not installed")
    def test_run_does_not_write_outside_target_dir(self):
        scan_dir = os.path.join(self.root, "scan")
        templates_dir = os.path.join(scan_dir, "chart", "templates")
        os.makedirs(templates_dir)
        with open(os.path.join(scan_dir, "chart", "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: test\nversion: 1.0.0\n")
        traversal = "../" * 30 + self.outside_dir.lstrip("/")
        with open(os.path.join(templates_dir, "cm.yaml"), "w") as f:
            f.write(
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n"
                f"---\n---\n# Source: {traversal}/file.yaml\nkey: value\n"
                f"---\n---\n# Source: {traversal}/.checkov.yml\nexternal-checks-dir: x\n"
            )

        runner = Runner()
        with mock.patch("checkov.helm.runner.parallel_runner.run_function",
                        side_effect=lambda func, items, *a, **k: [func(*i) if isinstance(i, tuple) else func(i)
                                                                  for i in items]):
            runner.run(root_folder=scan_dir, runner_filter=RunnerFilter(framework=["helm"]))

        # how the injected lines are laid out depends on the helm version, so only the file system is asserted here.
        # parsing of the documents around a skipped source is covered by the _parse_output tests above.
        self.assertEqual(os.listdir(self.outside_dir), [])


if __name__ == "__main__":
    unittest.main()
