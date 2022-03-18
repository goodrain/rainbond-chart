# Rainbond Helm Charts

You can deploy the Rainbond in Kubernetes using Helm Chart.

## Pre Requisites

* Recommended kubernetes version: 1.19+
* Root partition disk guarantee 50G+
* helm version: 3.0+
* Make sure the server `80, 443, 6060, 6443, 7070, 8443` ports are not in use

## Installation

Add `rainbond` helm charts repo

```
kubectl create namespace rbd-system
helm repo add rainbond https://openchart.goodrain.com/goodrain/rainbond
helm repo update
```
Installing helm chart

```
helm install rainbond rainbond/rainbond-cluster -n rbd-system
```


## Verify installation

* View pod status
```
kubectl get po -n rbd-system | grep rbd-app-ui
```
* Wait for the ``` rbd-app-ui ``` pod to be Running and the installation is successful.
* After successful installation, you can access the rainbond console through `` $gatewayIngressIPs:7070 ``.
