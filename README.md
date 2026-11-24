# Rainbond Helm Charts

You can deploy the Rainbond in Kubernetes using Helm Chart.

## Installation

See [Rainbond Installation Guide](https://www.rainbond.com/docs/installation/install-with-helm)

For upgrades of installations with legacy Console source overrides, see
[Console source mount migration](docs/console-source-mount-upgrade.md).

Chart regression checks: `python3 -m unittest discover -s tests -v` (requires Helm
and PyYAML), followed by `helm lint .` and `helm package .`.
