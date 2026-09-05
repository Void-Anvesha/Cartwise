import {onAuthStateChanged,signOut} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js';
let auth;try{({auth}=await import('./firebase-config.js'))}catch(error){location.replace('/login');throw error}
onAuthStateChanged(auth,user=>{
  if(!user){location.replace(`/login?next=${encodeURIComponent(location.pathname)}`);return}
  const fallback=(user.email||'Signed in').split('@')[0].replace(/\d+$/,'').replace(/(rastogi)$/i,' $1').replace(/[._-]+/g,' ').trim().replace(/\b\w/g,letter=>letter.toUpperCase());
  const displayName=user.displayName&&!user.displayName.includes('@')?user.displayName.trim():fallback;
  document.querySelectorAll('[data-user-email]').forEach(element=>{element.textContent=element.classList.contains('sidebar-account')?(user.email||'Signed in'):(displayName||'Signed in');element.title=user.email||''});
  document.querySelectorAll('[data-logout]').forEach(button=>button.onclick=async()=>{await signOut(auth);location.replace('/login')});
  document.body.classList.remove('auth-pending');
});
