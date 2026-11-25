from __future__ import annotations

import io
import itertools
import logging
import os
import re
import shutil
import subprocess  # nosec
import tempfile
from dataclasses import dataclass, field
from typing import Any, Type, TYPE_CHECKING
from urllib.parse import urlsplit
import yaml

from checkov.common.bridgecrew.check_type import CheckType
from checkov.common.graph.checks_infra.registry import BaseRegistry
from checkov.common.typing import LibraryGraphConnector
from checkov.common.graph.graph_builder.consts import GraphSource
from checkov.common.images.image_referencer import fix_related_resource_ids, Image
from checkov.common.output.report import Report
from checkov.common.parallelizer.parallel_runner import parallel_runner
from checkov.common.runners.base_runner import BaseRunner, filter_ignored_paths
from checkov.common.util.file_utils import _is_within
from checkov.helm.image_referencer.manager import HelmImageReferencerManager
from checkov.helm.partial_render import render_available_dependencies
from checkov.helm.registry import registry
from checkov.kubernetes.graph_builder.local_graph import KubernetesLocalGraph
from checkov.kubernetes.runner import Runner as k8_runner, _KubernetesContext, _KubernetesDefinitions
from checkov.runner_filter import RunnerFilter

if TYPE_CHECKING:
    from checkov.kubernetes.graph_manager import KubernetesGraphManager
    from networkx import DiGraph


# Regex matching remote helm repository URLs (http/https).
# OCI registries are not covered by this HTTP(S) repository allowlist.
_HELM_REMOTE_REPO = re.compile(r'^https?://', re.IGNORECASE)


def _get_chart_remote_repos(chart_dir: str) -> list[str]:
    """Return all remote repository URLs from Chart.yaml dependencies.

    Returns an empty list when the file cannot be read or has no remote repos.
    """
    chart_yaml_path = os.path.join(chart_dir, 'Chart.yaml')
    if not os.path.isfile(chart_yaml_path):
        return []
    try:
        with open(chart_yaml_path, 'r', encoding='utf-8') as f:
            doc = yaml.safe_load(f)
    except Exception:
        logging.debug(f"Could not parse {chart_yaml_path} for remote repo check.", exc_info=True)
        return []

    if not isinstance(doc, dict):
        return []

    remote_repos: list[str] = []
    for dep in doc.get('dependencies') or []:
        if not isinstance(dep, dict):
            continue
        repo = dep.get('repository', '')
        if isinstance(repo, str) and _HELM_REMOTE_REPO.match(repo):
            remote_repos.append(repo)
    return remote_repos


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _repo_matches_prefix(repo: str, prefix: str) -> bool:
    """Return True if the (lowercased) repo URL is covered by the (lowercased) allowlist prefix.

    For URLs the scheme, host and port must match exactly (so a lookalike host such as
    'charts.example.com.other.net' or port 8443 vs 844 does not match), and the repo path
    must start with the prefix path. Values without '://' keep the plain prefix comparison.
    """
    if "://" not in prefix or "://" not in repo:
        return repo.startswith(prefix)
    try:
        p, r = urlsplit(prefix), urlsplit(repo)
        if p.scheme != r.scheme or not p.hostname or p.hostname != r.hostname:
            return False
        default_port = _DEFAULT_PORTS.get(p.scheme)
        p_port = default_port if p.port is None else p.port
        r_port = default_port if r.port is None else r.port
        if p_port != r_port:
            return False
    except ValueError:
        return False  # e.g. invalid port
    return r.path.startswith(p.path)


def _get_blocked_helm_repos(chart_dir: str) -> list[str]:
    """Return dependency repos blocked by the CHECKOV_HELM_ALLOWED_REMOTE_REPOS allowlist.

    Not set → [] (allow all). Set → returns repos not matching any allowed prefix (these are blocked).
    To block all direct HTTP(S) repositories, use a non-matching value (e.g. 'none').
    """
    allowed_prefixes_raw = os.getenv("CHECKOV_HELM_ALLOWED_REMOTE_REPOS", "")
    if not allowed_prefixes_raw.strip():
        return []  # no allowlist configured → allow everything (original behaviour)

    allowed_prefixes = [p.strip().lower() for p in allowed_prefixes_raw.split(",") if p.strip()]
    if "*" in allowed_prefixes:
        return []  # '*' → allow everything
    remote_repos = _get_chart_remote_repos(chart_dir)
    return [repo for repo in remote_repos if not any(_repo_matches_prefix(repo.lower(), p) for p in allowed_prefixes)]


class HelmRenderError(Exception):
    def __init__(self, message: str, diagnostic: str = "") -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


@dataclass
class _ChartConversion:
    # Workers may run in separate processes, so return their data to the parent.
    template_mapping: dict[str, str] = field(default_factory=dict)
    rendered_files: set[str] = field(default_factory=set)
    error: str | None = None


class K8sHelmRunner(k8_runner):
    check_type = CheckType.HELM  # noqa: CCE003  # a static attribute

    def __init__(
        self,
        graph_class: Type[KubernetesLocalGraph] = KubernetesLocalGraph,
        db_connector: LibraryGraphConnector | None = None,
        source: str = GraphSource.KUBERNETES,
        graph_manager: KubernetesGraphManager | None = None,
        external_registries: list[BaseRegistry] | None = None
    ) -> None:
        super().__init__(graph_class, db_connector, source, graph_manager, external_registries)
        self.chart_dir_and_meta: list[tuple[str, dict[str, Any]]] = []
        self.pbar.turn_off_progress_bar()
        self.partial_files: dict[str, str] = {}
        self.original_root_dir = ''
        self.tmp_root_dir = ''
        self.template_mapping: dict[str, str] = {}

    def run(
        self,
        root_folder: str | None,
        external_checks_dir: list[str] | None = None,
        files: list[str] | None = None,
        runner_filter: RunnerFilter | None = None,
        collect_skip_comments: bool = True
    ) -> Report | list[Report]:
        runner_filter = runner_filter or RunnerFilter()
        report = Report(self.check_type)

        if not self.chart_dir_and_meta:
            return report
        if external_checks_dir:
            for directory in external_checks_dir:
                registry.load_external_checks(directory)
        try:
            chart_results = super().run(root_folder, external_checks_dir=external_checks_dir, runner_filter=runner_filter)

            if isinstance(chart_results, list):
                helm_report = next(
                    chart_result for chart_result in chart_results if chart_result.check_type == self.check_type
                )
                sca_image_report = next(
                    chart_result for chart_result in chart_results if chart_result.check_type == CheckType.SCA_IMAGE
                )
            else:
                helm_report = chart_results
                sca_image_report = None

            for record in helm_report.get_all_records():
                partial = self.partial_files.get(os.path.normpath(record.file_abs_path))
                if partial:
                    record.details.append(partial)

            if root_folder is not None:
                fix_report_paths(report=helm_report, tmp_dir=root_folder, template_mapping=self.template_mapping, original_root_folder=self.original_root_dir)
                if self.original_root_dir:
                    fix_related_resource_ids(report=sca_image_report, tmp_dir=self.original_root_dir)
                else:
                    fix_related_resource_ids(report=sca_image_report, tmp_dir=root_folder)

            return chart_results
        except Exception:
            logging.warning(f"Failed to run Kubernetes runner on charts {self.chart_dir_and_meta}", exc_info=True)
            report.add_parsing_error("Failed to scan rendered Helm manifests (see scanner log)")
            return report

    def get_image_report(self, root_folder: str | None, runner_filter: RunnerFilter) -> Report | None:
        if not self.graph_manager:
            return None
        return self.check_container_image_references(
            graph_connector=self.graph_manager.get_reader_endpoint(),
            root_path=self.original_root_dir,
            runner_filter=runner_filter,
        )

    def extract_images(
            self,
            graph_connector: DiGraph | None = None,
            definitions: None = None,
            definitions_raw: dict[str, list[tuple[int, str]]] | None = None
    ) -> list[Image]:
        if not graph_connector:
            # should not happen
            return []

        manager = HelmImageReferencerManager(graph_connector=graph_connector, original_root_dir=self.original_root_dir,
                                             temp_root_dir=self.tmp_root_dir)
        images = manager.extract_images_from_resources()

        return images


class Runner(BaseRunner[_KubernetesDefinitions, _KubernetesContext, "KubernetesGraphManager"]):
    check_type: str = CheckType.HELM  # noqa: CCE003  # a static attribute
    helm_command = 'helm'  # noqa: CCE003  # a static attribute
    system_deps = True  # noqa: CCE003  # a static attribute

    def __init__(self) -> None:
        super().__init__()
        self.file_names = ['Chart.yaml']
        self.target_folder_path = ''
        self.root_folder = ''
        self.runner_filter: "RunnerFilter | None" = None
        self.render_errors: list[str] = []
        self.partial_files: dict[str, str] = {}
        self.template_mapping: dict[str, str] = {}

    def get_k8s_target_folder_path(self) -> str:
        return self.target_folder_path

    @staticmethod
    def parse_helm_chart_details(chart_path: str) -> tuple[str, dict[str, Any] | None]:
        try:
            with open(os.path.join(chart_path, "Chart.yaml"), encoding="utf-8") as chart_file:
                chart_meta = yaml.safe_load(chart_file)
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            logging.warning("Failed to load chart metadata from %s/Chart.yaml", chart_path, exc_info=True)
            return chart_path, None
        return chart_path, chart_meta if isinstance(chart_meta, dict) else None

    def check_system_deps(self) -> str | None:
        # Ensure local system dependencies are available and of the correct version.
        # Returns framework names to skip if deps fail.
        logging.info(f"Checking necessary system dependencies for {self.check_type} checks.")
        try:
            proc = subprocess.Popen([self.helm_command, 'version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec
            o, _ = proc.communicate()
            oString = str(o, 'utf-8')
            if "Version:" in oString:
                helmVersionOutput = oString[oString.find(':') + 2: oString.find(',') - 1]
                if any(helmVersionOutput.startswith(s) for s in ("v3.", "v4.")):
                    logging.info(f"Found working version of {self.check_type} dependencies: {helmVersionOutput}")
                    return None
            else:
                return self.check_type
        except Exception as e:
            logging.info(f"Error running necessary tools to process {self.check_type} checks.")
            logging.debug(e)
        return self.check_type

    @staticmethod
    def _is_safe_source_path(target_dir: str, source: str) -> bool:
        """Check the path from a '# Source: ' line of the helm template output stays inside target_dir.

        Helm always renders it as a relative path inside the chart, ex. 'mychart/templates/deployment.yaml',
        so absolute paths and '..' segments can only come from the template content itself.
        """
        if not source or "\0" in source or os.path.isabs(source) or source.startswith(("/", "\\")):
            return False
        if ".." in re.split(r"[/\\]", source):
            return False
        # defense in depth - the resolved file path must be inside the target dir
        return bool(_is_within(target_dir, os.path.join(target_dir, source)))

    @staticmethod
    def _parse_output(target_dir: str, output: bytes, chart_dir: str, template_mapping: dict[str, str]) -> set[str]:
        rendered_files: set[str] = set()
        output_str = str(output, 'utf-8')
        reader = io.StringIO(output_str)
        cur_source_file = None
        cur_writer = None
        last_line_dashes = False
        line_num = 1
        for s in reader:
            s = s.rstrip()
            if s == '---':
                last_line_dashes = True
                continue

            if last_line_dashes:
                # The next line should contain a "Source" comment saying the name of the file it came from
                # So we will close the old file, open a new file, and write the dashes from last iteration plus this line

                if not s.startswith('# Source: '):
                    raise ValueError(f'Line {line_num}: Expected line to start with # Source: {s}')
                source = s[10:]
                if not Runner._is_safe_source_path(target_dir, source):
                    # Drop this document - its content must not be written outside of the target dir
                    logging.warning(f"Helm: skipping rendered document with an invalid source path: {source!r}")
                    if cur_writer:
                        cur_writer.close()
                    cur_writer = None
                    cur_source_file = None
                else:
                    if source != cur_source_file:
                        if cur_writer:
                            cur_writer.close()
                        file_path = os.path.join(target_dir, source)
                        rendered_files.add(os.path.normpath(file_path))
                        parent = os.path.dirname(file_path)
                        os.makedirs(parent, exist_ok=True)
                        cur_source_file = source
                        cur_writer = open(os.path.join(target_dir, source), 'a')

                    # Now extract the original template path from the source comment
                    # Format is typically: "chartname/templates/deployment.yaml"
                    # We need to extract just the "templates/deployment.yaml" part
                    template_path = source.split('/', 1)[1] if '/' in source else source

                    # Construct the path to the original template file
                    original_template = os.path.join(chart_dir, template_path)

                    if os.path.exists(original_template):
                        # Store mapping: temp file path (without prefix) -> original template path
                        template_mapping[os.path.join(target_dir, source).replace('//', '/')] = original_template

                if cur_writer:
                    cur_writer.write('---' + os.linesep)
                    cur_writer.write(s + os.linesep)

                last_line_dashes = False
            else:
                if s.startswith('# Source: '):
                    raise ValueError(f'Line {line_num}: Unexpected line starting with # Source: {s}')

                if not cur_writer:
                    continue
                else:
                    cur_writer.write(s + os.linesep)

            line_num += 1

        if cur_writer:
            cur_writer.close()
        return rendered_files

    @staticmethod
    def _get_target_dir(chart_item: tuple[str, dict[str, Any]], root_folder: str, target_folder_path: str) -> str | None:
        (chart_dir, chart_meta) = chart_item
        target_dir = chart_dir.replace(root_folder, f'{target_folder_path}/')
        target_dir.replace("//", "/")
        chart_name = chart_meta.get('name', chart_meta.get('Name'))
        if not isinstance(chart_name, str) or not chart_name:
            logging.info(
                f"Error parsing chart located {chart_dir}, chart has no name available",
                exc_info=True,
            )
            return None
        if target_dir.endswith('/'):
            target_dir = target_dir[:-1]
        if target_dir.endswith(chart_name):
            target_dir = target_dir[:-len(chart_name)]
        return target_dir

    @staticmethod
    def get_binary_output_from_directory(chart_dir: str, target_dir: str, helm_command: str,
                                         runner_filter: RunnerFilter, timeout: int = 3600) \
            -> tuple[bytes, tuple[str, dict[str, Any]]] | tuple[None, None]:
        _, chart_meta = Runner.parse_helm_chart_details(chart_dir)
        chart_item = (chart_dir, chart_meta or {})
        return Runner.get_binary_output(chart_item, target_dir, helm_command, runner_filter, timeout)

    @staticmethod
    def get_binary_output(
        chart_item: tuple[str, dict[str, Any]], target_dir: str, helm_command: str, runner_filter: RunnerFilter, timeout: int = 3600
    ) -> tuple[bytes, tuple[str, dict[str, Any]]] | tuple[None, None]:
        try:
            return Runner._render_chart(chart_item, helm_command, runner_filter, timeout), chart_item
        except HelmRenderError as exc:
            logging.warning("Failed processing Helm chart %s: %s", chart_item[0], exc)
            return None, None

    @staticmethod
    def _render_chart(
        chart_item: tuple[str, dict[str, Any]], helm_command: str, runner_filter: RunnerFilter, timeout: int
    ) -> bytes:
        chart_dir, chart_meta = chart_item
        if not isinstance(chart_meta, dict):
            raise HelmRenderError("invalid chart metadata")
        logging.info("Processing Helm chart %s", chart_dir)
        try:
            deps = subprocess.run([helm_command, 'dependency', 'list', chart_dir],  # nosec
                                  capture_output=True, timeout=timeout, check=False)
            logging.debug("Helm dependency list for %s: %s", chart_dir, deps.stdout.decode(errors='replace'))
            if deps.stderr:
                logging.warning("Helm dependency list for %s: %s", chart_dir, deps.stderr.decode(errors='replace').strip())

            blocked_repos = _get_blocked_helm_repos(chart_dir)
            if blocked_repos:
                raise HelmRenderError(f"{len(blocked_repos)} dependency repo(s) not in CHECKOV_HELM_ALLOWED_REMOTE_REPOS allowlist")

            args = [helm_command, 'template', '--dependency-update', chart_dir]
            for var_file in runner_filter.var_files or []:
                args.extend(['--values', var_file])
            result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)  # nosec
        except subprocess.TimeoutExpired as exc:
            raise HelmRenderError(f"Helm timed out after {timeout} seconds") from exc
        except OSError as exc:
            raise HelmRenderError(f"could not run Helm: {exc}") from exc

        diagnostic = result.stderr.decode(errors='replace').strip()
        if diagnostic:
            logging.warning("Helm template for %s: %s", chart_dir, diagnostic)
        if result.returncode:
            message = f"helm template exited {result.returncode}"
            if "could not download" in diagnostic and "401" in diagnostic:
                message += " (dependency download returned HTTP 401 Unauthorized)"
            raise HelmRenderError(message, diagnostic)
        return result.stdout

    @staticmethod
    def _convert_chart_to_k8s(
        chart_item: tuple[str, dict[str, Any]],
        root_folder: str,
        target_folder_path: str,
        helm_command: str,
        runner_filter: RunnerFilter,
    ) -> _ChartConversion:
        result = _ChartConversion()
        chart_dir, _ = chart_item
        chart_path = os.path.join(chart_dir, "Chart.yaml")
        target_dir = Runner._get_target_dir(chart_item, root_folder, target_folder_path)
        if not target_dir:
            result.error = f"{chart_path}: invalid chart metadata or missing chart name"
            return result

        try:
            output = Runner._render_chart(chart_item, helm_command, runner_filter, timeout=3600)
        except HelmRenderError as exc:
            result.error = f"{chart_path}: {exc}"
            recovered = None
            if exc.diagnostic:
                recovered = render_available_dependencies(
                    chart_dir, helm_command, runner_filter.var_files or [], 3600, exc.diagnostic,
                    blocked_repos=_get_blocked_helm_repos,
                )
            if recovered is None:
                logging.warning("Incomplete Helm scan for %s", result.error)
                return result
            output, omitted = recovered
            result.error += f" (partial render, omitted dependencies: {', '.join(omitted)})"
            logging.warning("Incomplete Helm scan for %s", result.error)

        try:
            result.rendered_files = Runner._parse_output(target_dir, output, chart_dir, result.template_mapping)
        except (OSError, ValueError, RuntimeError) as exc:
            logging.warning("Error parsing rendered output for %s", chart_dir, exc_info=True)
            result.error = f"{result.error or chart_path}: failed to read rendered manifests ({type(exc).__name__})"
        return result

    @staticmethod
    def _get_processed_chart_dir_and_meta(
        chart_dir_and_meta: list[tuple[str, dict[str, Any]]], root_folder: str
    ) -> list[tuple[str, dict[str, Any]]]:
        processed_chart_dir_and_meta = []
        for chart_dir, chart_meta in chart_dir_and_meta:
            processed_chart_dir_and_meta.append((chart_dir.replace(root_folder, ""), chart_meta))
        return processed_chart_dir_and_meta

    def convert_helm_to_k8s(
        self, root_folder: str | None, files: list[str] | None, runner_filter: RunnerFilter
    ) -> list[tuple[Any, dict[str, Any]]]:
        self.root_folder = root_folder or ""
        self.runner_filter = runner_filter
        self.target_folder_path = tempfile.mkdtemp()
        self.render_errors = []
        self.template_mapping = {}
        self.partial_files = {}
        chart_dirs = find_chart_directories(root_folder, files, runner_filter.excluded_paths)
        metadata = parallel_runner.run_function(func=Runner.parse_helm_chart_details, items=chart_dirs)
        chart_dir_and_meta = [(directory, meta or {}) for directory, meta in metadata]
        chart_items = [(item, self.root_folder, self.target_folder_path, self.helm_command, runner_filter)
                       for item in chart_dir_and_meta]
        for conversion in parallel_runner.run_function(func=Runner._convert_chart_to_k8s, items=chart_items):
            self.template_mapping.update(conversion.template_mapping)
            if conversion.error:
                self.render_errors.append(conversion.error)
                self.partial_files.update(dict.fromkeys(conversion.rendered_files, conversion.error))
        self.render_errors.sort()
        return Runner._get_processed_chart_dir_and_meta(chart_dir_and_meta, self.root_folder)

    def remove_target_folder(self) -> None:
        try:
            shutil.rmtree(self.target_folder_path)  # delete directory
        except OSError as exc:
            logging.debug("failed to remove helm target folder path", exc_info=exc)

    def run(
        self,
        root_folder: str | None,
        external_checks_dir: list[str] | None = None,
        files: list[str] | None = None,
        runner_filter: RunnerFilter | None = None,
        collect_skip_comments: bool = True
    ) -> Report | list[Report]:
        runner_filter = runner_filter or RunnerFilter()
        if not runner_filter.show_progress_bar:
            self.pbar.turn_off_progress_bar()

        k8s_runner = K8sHelmRunner()
        try:
            k8s_runner.chart_dir_and_meta = self.convert_helm_to_k8s(root_folder, files, runner_filter)
            k8s_runner.template_mapping = self.template_mapping
            k8s_runner.partial_files = self.partial_files
            k8s_runner.original_root_dir = str(root_folder)
            k8s_runner.tmp_root_dir = self.get_k8s_target_folder_path()
            report = k8s_runner.run(k8s_runner.tmp_root_dir, external_checks_dir=external_checks_dir, runner_filter=runner_filter)
            helm_report = next(r for r in report if r.check_type == self.check_type) if isinstance(report, list) else report
            helm_report.add_parsing_errors(self.render_errors)
            self.graph_manager = k8s_runner.graph_manager
            return report
        finally:
            self.remove_target_folder()


def fix_report_paths(report: Report, tmp_dir: str, template_mapping: dict[str, str], original_root_folder: str) -> None:
    """
    Fix file paths in the report to point to original Helm template files instead of temporary K8s manifests.

    Args:
        report: The report containing checks with file paths to fix
        tmp_dir: The temporary directory containing the rendered K8s manifests
        template_mapping: Mapping of temporary paths to original template paths
        original_root_folder: The original Helm chart folder
    """
    for check in itertools.chain(report.failed_checks, report.passed_checks):
        # First remove the tmp_dir prefix
        tmp_path = check.repo_file_path

        # Then check if we have a mapping to the original template file
        if tmp_path in template_mapping:
            file_abs_path = template_mapping[tmp_path]
            repo_file_path = file_abs_path.replace(original_root_folder, '', 1)
            check.repo_file_path = repo_file_path
            check.file_path = repo_file_path
            check.file_abs_path = file_abs_path
        else:
            check.repo_file_path = tmp_path.replace(tmp_dir, '', 1)

    # Update resources in the report
    new_resources = set()
    for resource in report.resources:
        resource_file_path = resource.split(':')[0]
        resource_id = resource.split(':')[1]
        if resource_file_path in template_mapping:
            new_resources.add(f'{template_mapping[resource_file_path]}:{resource_id}')
        else:
            new_resources.add(resource.replace(tmp_dir, '', 1))

    report.resources = new_resources


def get_skipped_checks(entity_conf: dict[str, Any]) -> list[dict[str, str]]:
    skipped = []
    metadata = {}
    if not isinstance(entity_conf, dict):
        return skipped
    if entity_conf["kind"] == "containers" or entity_conf["kind"] == "initContainers":
        metadata = entity_conf["parent_metadata"]
    else:
        if "metadata" in entity_conf.keys():
            metadata = entity_conf["metadata"]
    if "annotations" in metadata.keys() and metadata["annotations"] is not None:
        for key in metadata["annotations"].keys():
            skipped_item = {}
            if "checkov.io/skip" in key or "bridgecrew.io/skip" in key:
                if "CKV_K8S" in metadata["annotations"][key]:
                    if "=" in metadata["annotations"][key]:
                        (skipped_item["id"], skipped_item["suppress_comment"]) = metadata["annotations"][key].split("=")
                    else:
                        skipped_item["id"] = metadata["annotations"][key]
                        skipped_item["suppress_comment"] = "No comment provided"
                    skipped.append(skipped_item)
                else:
                    logging.info(f"Parse of Annotation Failed for {metadata['annotations'][key]}: {entity_conf}")
                    continue
    return skipped


def find_chart_directories(root_folder: str | None, files: list[str] | None, excluded_paths: list[str]) -> list[str]:
    chart_directories = []
    if not excluded_paths:
        excluded_paths = []
    if files:
        logging.info('Running with --file argument; checking for Helm Chart.yaml files')
        for file in files:
            if os.path.basename(file) == 'Chart.yaml':
                chart_directories.append(os.path.dirname(file))

    if root_folder:
        for root, d_names, f_names in os.walk(root_folder):
            filter_ignored_paths(root, d_names, excluded_paths)
            filter_ignored_paths(root, f_names, excluded_paths)
            if 'Chart.yaml' in f_names:
                chart_directories.append(root)

    return chart_directories
