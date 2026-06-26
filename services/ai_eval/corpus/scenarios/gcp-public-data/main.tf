resource "google_storage_bucket" "data" {
  name                        = "acme-analytics-exports"
  location                    = "EU"
  force_destroy               = true
  uniform_bucket_level_access = true
}

# Grants the entire internet read access to every object in the bucket.
resource "google_storage_bucket_iam_member" "public" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_sql_database_instance" "analytics" {
  name             = "acme-analytics"
  database_version = "POSTGRES_16"
  region           = var.region

  deletion_protection = false

  settings {
    tier = "db-custom-4-16384"

    ip_configuration {
      ipv4_enabled = true

      authorized_networks {
        name  = "world"
        value = "0.0.0.0/0"
      }
    }

    backup_configuration {
      enabled = false
    }
  }
}

resource "google_sql_user" "admin" {
  name     = "dbadmin"
  instance = google_sql_database_instance.analytics.name
  password = var.db_password
}
