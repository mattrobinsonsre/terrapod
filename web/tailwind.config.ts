import type { Config } from 'tailwindcss'

const config: Config = {
  darkMode: 'class',
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f5f3ff',
          100: '#ede9fe',
          200: '#ddd6fe',
          300: '#c4b5fd',
          400: '#a78bfa',
          500: '#8b5cf6',
          600: '#7c3aed',
          700: '#6d28d9',
          800: '#5b21b6',
          900: '#4c1d95',
        },
      },
      boxShadow: {
        'glow-sm': '0 0 10px rgba(124, 58, 237, 0.3)',
        'glow': '0 0 20px rgba(124, 58, 237, 0.4)',
        'glow-lg': '0 0 30px rgba(124, 58, 237, 0.5)',
      },
    },
  },
  plugins: [],
}

export default config
