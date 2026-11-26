---
layout: default
published: true
title: Helm
nav_order: 9
---

# Scan Helm charts with Checkov

Checkov is able to autodetect helm charts by the presence of a `Chart.yaml` file, if found, the helm framework will automatically be used to template out the helm chart (with it's default values) into resulting Kubernetes manifests, which will then be scanned by all Checkovs' Kubernetes policies.


```
       _               _
   ___| |__   ___  ___| | _______   __
  / __| '_ \ / _ \/ __| |/ / _ \ \ / /
 | (__| | | |  __/ (__|   < (_) \ V /
  \___|_| |_|\___|\___|_|\_\___/ \_/

By Prisma Cloud | version: x.x.x 

helm scan results:

Passed checks: 370, Failed checks: 90, Skipped checks: 0

Check: CKV_K8S_27: "Do not expose the docker daemon socket to containers"
	PASSED for resource: Deployment.RELEASE-NAME-nextcloud.default
	File: /nextcloud/templates/deployment.yaml:3-107
	Guide: https://docs.prismacloud.io/en/enterprise-edition/policy-reference/kubernetes-policies/kubernetes-policy-index/bc-k8s-26


...
```

To do this, we use the helm binary.\
If a helm binary greater than v3 is not available via the users `$PATH`, Checkov will automatically skip the helm framework, with a message informing the user.\
Checkov will then run with exactly the same behaviour as if running with `checkov --skip-framework helm`.

```
The following frameworks will automatically be disabled due to missing system dependencies: helm
```

This auto-detection behaviour is to protect existing CI pipelines which pull the latest version of Checkov, which may not have the helm binary available.

If you have custom value files, and scanning a Chart (a directory containing a `Chart.yaml` file), you can tell checkov to use your values while creating the kubernetes manifests from the chart, to do this, use the following (for example, scanning a gocd chart directory):

```
checkov -d ./testdir/gocd --framework helm --var-file ./testdir/gocd.yaml
```

in the example below, we can see the difference between the default `gocd` version `1.39.4` Helm chart scan, compared to a scan using the following `gocd.yaml` variables file:


```
checkov -d ./testdir/gocd --framework helm --quiet --compact -c CKV_K8S_15

helm scan results:

Passed checks: 0, Failed checks: 3, Skipped checks: 0

Check: CKV_K8S_15: "Image Pull Policy should be Always"
        FAILED for resource: Deployment.default.RELEASE-NAME-gocd-agent
        File: /gocd/templates/gocd-agent-deployment.yaml:3-46

Check: CKV_K8S_15: "Image Pull Policy should be Always"
        FAILED for resource: Deployment.default.RELEASE-NAME-gocd-server
        File: /gocd/templates/gocd-server-deployment.yaml:3-90

Check: CKV_K8S_15: "Image Pull Policy should be Always"
        FAILED for resource: Pod.default.RELEASE-NAME-gocd-test-ku4xn
        File: /gocd/templates/tests/gocd-test.yaml:3-44
```


```
checkov -d ./testdir/gocd --framework helm --quiet --compact --var-file ./testdir/gocd.yaml -c CKV_K8S_15

helm scan results:

Passed checks: 2, Failed checks: 1, Skipped checks: 0

Check: CKV_K8S_15: "Image Pull Policy should be Always"
        FAILED for resource: Pod.default.RELEASE-NAME-gocd-test-o6jdi
        File: /gocd/templates/tests/gocd-test.yaml:3-44

```


# Third party Helm charts
## Scan Helm values.yaml files without a locally developed chart with Checkov

If you are consuming third party charts, it is unlikely you will have a `Chart.yaml` file for Checkov to auto-detect.

For example, you may have run: 

```
helm repo add gocd https://gocd.github.io/helm-chart
helm inspect values gocd/gocd > gocd.yaml
```

Then edited your custom values in `gocd.yaml` to suit your deployment needs.

In this case, you can use your custom values from your values file (`gocd.yml` in this case) to generate kubernetes output with Helm, and pass the templated output to checkov to receive the same policy information. This would also be trivial to set up in CI for automated scanning of this scenario:

```
helm template gocd/gocd -f gocd.yaml > k8s-template.yaml
checkov -f k8s-template.yaml --framework kubernetes --skip-check CKV_K8S_21
```

Note we skip check `CKV_K8S_21` for this process, which alerts on default namespace usage within Kubernetes manifests.
Since helm manages our namespaces, we always skip this internally when using the helm framework, so we want to replicate the same behaviour here.

## Incomplete charts

Checkov uses Helm's exit code to decide whether rendering succeeded. When Helm returns success, Checkov scans the manifests even if Helm wrote warnings or registry download progress to stderr.

If Helm cannot resolve a direct dependency of a v2 chart, Checkov can render the parent and available subcharts in a temporary copy. The retry retains the original values files and their order. When dependency downloads are enabled, Helm identifies the failed dependency, and the remaining declarations can be resolved safely, Checkov also attempts to download the remaining dependencies. If the reduced chart still needs a missing helper or cannot be rendered, Checkov reports the chart error and continues with other charts.

Findings from a partial render carry a note in `details` naming the omitted dependencies. The Helm report includes the chart in `results.parsing_errors`, including in quiet JSON output. These findings describe the resources produced by the reduced chart. Missing subcharts can affect values, templates and policies that check relationships between resources.

An incomplete Helm scan exits with code 1 by default, even when there are zero policy findings. The usual `--soft-fail` option and explicit `CKV_PARSE_ERROR_FAIL` setting control this behavior. If the shell runs `helm dependency build ./chart && checkov -d ./chart --framework helm`, the shell stops at a failed build before Checkov can perform recovery.

## Dependency Repository Security

By default, Checkov runs `helm template` without `--dependency-update`, so Helm never downloads a dependency. Charts render from what the scanned directory holds: a dependency bundled under `charts/` (a packaged `.tgz` or an unpacked chart) is used whatever repository its declaration in `Chart.yaml` or `requirements.yaml` names, including HTTPS, OCI and private registries. A dependency that is not bundled is reported, and the chart is scanned as a partial render (see [Incomplete charts](#incomplete-charts)). Bundled subcharts bring their own bundled dependencies, so nested dependencies follow the same rule. `file://` dependencies are not packaged from their source folder in this mode; bundle them, or enable downloads.

To let Helm download missing dependencies, set `CHECKOV_HELM_DEPENDENCY_UPDATE=true`. Checkov then runs `helm template --dependency-update`, which writes the downloaded charts into the chart's `charts/` folder. Private repositories need Helm's own registry login or repository configuration.

When downloads are enabled, `CHECKOV_HELM_ALLOWED_REMOTE_REPOS` restricts them to a comma-separated list of trusted URL prefixes, set in CI. It covers HTTPS and OCI repositories declared in `Chart.yaml` or `requirements.yaml`. If a declared repository does not match, Checkov skips that chart and reports a parsing error. Without downloads the allowlist is not consulted, because nothing is downloaded.

### Usage (set as a CI environment variable, not in scanned files)

```bash
# Default: render bundled dependencies only, never download:
checkov -d ./myapp --framework helm

# Download missing dependencies, only from these prefixes:
CHECKOV_HELM_DEPENDENCY_UPDATE=true \
CHECKOV_HELM_ALLOWED_REMOTE_REPOS=https://charts.myorg/,oci://registry.myorg/charts \
  checkov -d ./myapp --framework helm
```

### Example warning when a dependency repo is blocked

```
WARNING: Incomplete Helm scan for /path/to/chart/Chart.yaml: 1 dependency repo(s) not in CHECKOV_HELM_ALLOWED_REMOTE_REPOS allowlist
```

