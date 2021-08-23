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
--version 5.3.3 \
--set pvc.storageClassName=my-storageclass \
--set pvc.storageSize=5Gi
```

1. `pvc.storageClassName` Set StorageClassName in the cluster.
2. `pvc.storageSize`  Sets the volume size.

Nodeport 30707 is enabled by default, if you want to customize the port, please refer to :point_down:

```shell
helm install --name my-console rainbond/rainbond-console \
--version 5.3.3 \
--set allinone.svc.nodePort=30707
```

If you want to customize The parameters,The chart can be customized using The following configurable parameters.

| Parameter                                                    | Description                                                  | Default                                             |
| :----------------------------------------------------------- | :----------------------------------------------------------- | :-------------------------------------------------- |
| `pvc.storageClassName`                                       | The StorageClass required for the RainBond component         | ""                                                  |
| `pvc.storageSize`                                            | To apply for PV Size                                         | 5Gi                                                 |
| `redis.fullname`                                             | Redis full name                                              | rainbond-redis                                      |
| `redis.image.repository`                                     | Redis image repository                                       | redis                                               |
| `redis.image.pullPolicy`                                     | Redis image Pull strategy                                    | IfNotPresent                                        |
| `redis.image.tag`                                            | Redis image tag                                              | 4.0.12                                              |
| `redis.password`                                             | Redis Password                                               | 123456                                              |
| `redis.podAnnotations`                                       | Annotation to be added to Redis pods                         | {}                                                  |
| `redis.selectorLabels`                                       | Set the Redis Labels                                         | rainbond: redis                                     |
| `redis.resources`                                            | Redis resource requests and limits                           | {}                                                  |
| `redis.nodeSelector`                                         | Node labels for pod assignment                               | {}                                                  |
| `redis.affinity`                                             | Defines affinities and anti-affinities for pods as defined in: https://kubernetes.io/docs/concepts/configuration/assign-pod-node/#affinity-and-anti-affinity preferences | {}                                                  |
| `mysql.fullname`                                             | Mysql full name                                              | rainbond-mysql                                      |
| `mysql.image.repository`                                     | Mysql image repository                                       | mysql                                               |
| `mysql.image.pullPolicy`                                     | Mysql image Pull strategy                                    | IfNotPresent                                        |
| `mysql.image.tag`                                            | Mysql image tag                                              | 5.7.23                                              |
| `mysql.secret.user` `mysql.secret.password` `mysql.secret.rootpassword` `mysql.secret.database` | Specify  MySQL account password and database                 |                                                     |
| `mysql.podAnnotations`                                       | Annotation to be added to Mysql pods                         | {}                                                  |
| `mysql.selectorLabels`                                       | Set the Mysql Labels                                         | rainbond: mysql                                     |
| `mysql.resources`                                            | Mysql resource requests and limits                           | {}                                                  |
| `mysql.nodeSelector`                                         | Node labels for pod assignment                               | {}                                                  |
| `mysql.affinity`                                             | Defines affinities and anti-affinities for pods as defined in: https://kubernetes.io/docs/concepts/configuration/assign-pod-node/#affinity-and-anti-affinity preferences | {}                                                  |
| `allinone.fullname`                                          | Allinone Console full name                                   | rainbond-allinone                                   |
| `allinone.replicasCount`                                     | Set the number of copies                                     | 1                                                   |
| `allinone.image.repository`                                  | Allinone Console image repository                            | registry.cn-hangzhou.aliyuncs.com/goodrain/rainbond |
| `allinone.image.pullPolicy`                                  | Allinone Console image Pull strategy                         | IfNotPresent                                        |
| `allinone.image.tag`                                         | Allinone Console image tag                                   | v5.3.3-release-allinone                             |
| `allinone.svc.type`                                          | Allinone Console Service Type                                | NodePort                                            |
| `allinone.svc.nodePort`                                      | Allinone Console Service Port                                | 30707                                               |
| `allinone.podAnnotations`                                    | Annotation to be added to Allinone Console pods              | {}                                                  |
| `allinone.selectorLabels`                                    | Set the Allinone Console Labels                              | rainbond: allinone                                  |
| `allinone.resources`                                         | Allinone Console resource requests and limits                | {}                                                  |
| `allinone.nodeSelector`                                      | Node labels for pod assignment                               | {}                                                  |
| `allinone.affinity`                                          | Defines affinities and anti-affinities for pods as defined in: https://kubernetes.io/docs/concepts/configuration/assign-pod-node/#affinity-and-anti-affinity preferences | {}                                                  |

## Related repositorys

- [Rainbond-Console](https://github.com/goodrain/rainbond-console)
- [Rainbond-Console-UI](https://github.com/goodrain/rainbond-ui)
- [Rainbond-Operator](https://github.com/goodrain/rainbond-operator)
- [Rainbond-Builder](https://github.com/goodrain/builder)
- [Rainbond-Docs](https://github.com/goodrain/rainbond-docs)

## License

Rainbond follow LGPL-3.0 license，Details see[LICENSE](https://github.com/goodrain/rainbond/blob/master/LICENSE) and [Licensing](https://github.com/goodrain/rainbond/blob/master/Licensing.md)

