# Rainbond Console Helm Charts

You can deploy the Rainbond Console in Kubernets using Helm Chart.

## Pre Requisites

* Recommended kubernetes version: 1.19+

* helm version: 3.0+

## Installation

Add `rainbond-operator` helm charts repo

```
kubectl create namespace rbd-system
helm repo add rainbond-operator https://openchart.goodrain.com/goodrain/rainbond
helm repo update
```
Installing helm chart

Install the controller first,then install the console

```
helm install rainbond-operator rainbond/rainbond-operator -n rbd-system --set operator.image.name=registry.cn-hangzhou.aliyuncs.com/yangkaa/rainbond-operator --set operator.image.tag=v2.2.0-dev
```

Enable the console to connect to the kubernetes cluster
```
helm install rainbond-cluster rainbond/rainbond-cluster -n rbd-system
```
## License

Rainbond follow LGPL-3.0 license，Details see[LICENSE](https://github.com/goodrain/rainbond/blob/master/LICENSE) and [Licensing](https://github.com/goodrain/rainbond/blob/master/Licensing.md)
