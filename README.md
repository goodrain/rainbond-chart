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
helm install rainbond-operator ./operator-chart2 -n rbd-system --set operator.image.name=registry.cn-hangzhou.aliyuncs.com/yangkaa/rainbond-operator --set operator.image.tag=v2.2.0-dev
```
