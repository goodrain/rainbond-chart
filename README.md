# Rainbond Console Helm Charts

You can deploy the Rainbond Console in Kubernets using Helm Chart.

## Pre Requisites

* Kubernetes version cannot be earlier than 1.19

* StorageClass is required within the cluster

  > ref: https://artifacthub.io/packages/helm/kvaps/nfs-server-provisioner

## Installation

Add `rainbond-console` helm charts repo

```
helm repo add rainbond https://openchart.goodrain.com/goodrain/rainbond
helm repo update
```

Installing helm chart

> You need to have dynamic storage available on your Kubernetes cluster(StorageClass).
>
> If not, please refer [Install NFS-Server](https://artifacthub.io/packages/helm/kvaps/nfs-server-provisioner) .

```shell
helm install --name my-console rainbond/rainbond-console \
--version 5.5.0 \
--set pvc.storageClassName=my-storageclass \
--set pvc.storageSize=5Gi
```

1. `pvc.storageClassName` Set StorageClassName in the cluster.
2. `pvc.storageSize`  Sets the volume size.

Nodeport 30707 is enabled by default, if you want to customize the port, please refer to :point_down:

```shell
helm install --name my-console rainbond/rainbond-console \
--version 5.5.0 \
--set allinone.svc.nodePort=30707
```

