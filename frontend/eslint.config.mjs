import { FlatCompat } from "@eslint/eslintrc";

// eslint-config-next still ships in eslintrc format, so it is bridged into flat config.
const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default config;
