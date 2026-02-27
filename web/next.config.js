/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: process.env.API_URL
          ? `${process.env.API_URL}/api/:path*`
          : 'http://localhost:8001/api/:path*',
      },
      {
        source: '/.well-known/:path*',
        destination: process.env.API_URL
          ? `${process.env.API_URL}/.well-known/:path*`
          : 'http://localhost:8001/.well-known/:path*',
      },
      {
        source: '/v1/:path*',
        destination: process.env.API_URL
          ? `${process.env.API_URL}/v1/:path*`
          : 'http://localhost:8001/v1/:path*',
      },
    ]
  },
}

module.exports = nextConfig
