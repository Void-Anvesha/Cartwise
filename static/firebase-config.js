import {initializeApp} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-app.js';
import {getAuth} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js';

const response=await fetch('/api/firebase-config');
if(!response.ok)throw new Error('Firebase configuration could not be loaded.');
const config=await response.json();
if(!config.apiKey||!config.authDomain||!config.projectId||!config.appId)throw new Error('Firebase Authentication is not configured. Add the FIREBASE_* values to .env and restart the server.');
export const auth=getAuth(initializeApp(config));
