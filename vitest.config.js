import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    alias: {
      'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js': 'firebase/firestore',
      'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js': 'firebase/app',
      'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js': 'firebase/auth',
    },
  },
  test: {
    include: ['site/__tests__/**/*.test.js'],
  },
});
