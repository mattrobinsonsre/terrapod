module github.com/mattrobinsonsre/terrapod/publish

go 1.26

require (
	github.com/ProtonMail/go-crypto v1.4.1
	github.com/mattrobinsonsre/terrapod/go-terrapod v0.0.0-00010101000000-000000000000
)

require (
	github.com/cloudflare/circl v1.6.2 // indirect
	golang.org/x/crypto v0.41.0 // indirect
	golang.org/x/sys v0.35.0 // indirect
)

replace github.com/mattrobinsonsre/terrapod/go-terrapod => ../go-terrapod
