resource "kubernetes_namespace" "apps" {
  metadata {
    name = "batch"
  }
}

resource "kubernetes_deployment" "worker" {
  metadata {
    name      = "log-shipper"
    namespace = kubernetes_namespace.apps.metadata[0].name
  }

  spec {
    replicas = 3

    selector {
      match_labels = { app = "log-shipper" }
    }

    template {
      metadata {
        labels = { app = "log-shipper" }
      }

      spec {
        # Shares the host network namespace and mounts the host root filesystem.
        host_network = true

        container {
          name  = "shipper"
          image = "acme/log-shipper:1.4.2"

          security_context {
            privileged                 = true
            allow_privilege_escalation = true
            run_as_user                = 0
          }

          volume_mount {
            name       = "host-root"
            mount_path = "/host"
          }
        }

        volume {
          name = "host-root"
          host_path {
            path = "/"
          }
        }
      }
    }
  }
}
