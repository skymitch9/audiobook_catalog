/**
 * Firebase Configuration and Initialization
 * 
 * This file initializes Firebase and exports the Firestore database instance.
 */

import { initializeApp } from 'firebase/app';
import { getFirestore } from 'firebase/firestore';

// Firebase project configuration
const firebaseConfig = {
  apiKey: "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y",
  authDomain: "audiobook-catalog.firebaseapp.com",
  projectId: "audiobook-catalog",
  storageBucket: "audiobook-catalog.firebasestorage.app",
  messagingSenderId: "68492219785",
  appId: "1:68492219785:web:7cbe57dda8712377f0bd58"
};

// Initialize Firebase
const app = initializeApp(firebaseConfig);

// Initialize Firestore
export const db = getFirestore(app);

export default app;
