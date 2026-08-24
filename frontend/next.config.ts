import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The backend owns the API. Proxying through Next keeps the browser on one origin, so the
  // session cookie is first-party and CORS never enters the picture in development.
  async rewrites() {
    const backend = process.env.BACKEND_URL ?? "http://localhost:8000";
    return [{ source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` }];
  },
};

export default nextConfig;
