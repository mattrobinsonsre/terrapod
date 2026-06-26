resource "azurerm_resource_group" "data" {
  name     = "${var.environment}-analytics-rg"
  location = var.location

  tags = {
    environment = var.environment
    managed_by  = "opentofu"
  }
}

resource "azurerm_mssql_server" "analytics" {
  name                = "${var.environment}-analytics-sql"
  resource_group_name = azurerm_resource_group.data.name
  location            = azurerm_resource_group.data.location
  version             = "12.0"

  administrator_login          = "sqladmin"
  administrator_login_password = var.sql_admin_password

  # Reachable from the public internet.
  public_network_access_enabled = true
  minimum_tls_version           = "1.0"
}

# Firewall rule that allows the entire IPv4 internet to reach the SQL server.
resource "azurerm_mssql_firewall_rule" "allow_all" {
  name             = "allow-all"
  server_id        = azurerm_mssql_server.analytics.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "255.255.255.255"
}

resource "azurerm_mssql_database" "analytics" {
  name      = "analytics"
  server_id = azurerm_mssql_server.analytics.id
  sku_name  = "S1"

  # No long-term retention; storage not customer-managed-key encrypted.
  storage_account_type = "Local"

  tags = {
    environment = var.environment
  }
}
