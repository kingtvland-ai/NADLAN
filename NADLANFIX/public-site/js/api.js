// NADLANFIX Public Site - API Layer
// Communicates with the PlanWatch backend

const API_BASE = window.API_BASE || '/api';
const TOKEN_KEY = 'nadlanfix_token';

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

function isLoggedIn() {
  return !!getToken();
}

async function api(path, options = {}) {
  const token = getToken();
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  };

  const config = {
    ...options,
    headers,
  };

  const maxRetries = 2;
  const baseDelay = 500;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await fetch(`${API_BASE}${path}`, config);
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || `HTTP ${response.status}`);
      }

      return data;
    } catch (error) {
      const isLastAttempt = attempt === maxRetries;
      const isRetryable = error.name === 'TypeError' || (error.message && error.message.includes('HTTP 5'));
      if (isLastAttempt || !isRetryable) {
        console.error(`API Error [${path}]:`, error);
        throw error;
      }
      const delay = baseDelay * Math.pow(2, attempt);
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }
}

// Auth
async function login(username, password) {
  const data = await api('/user/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  setToken(data.token);
  return data;
}

function logout() {
  setToken(null);
}

// Search
async function searchLocalities(query = '') {
  return api(`/localities?limit=50&q=${encodeURIComponent(query)}`);
}

async function searchParcel(street, number) {
  const address = `${street} ${number}`.trim();
  return api(`/parcel?address=${encodeURIComponent(address)}`);
}

// Listings
async function getCombinedListings(params = {}) {
  const query = new URLSearchParams(params).toString();
  return api(`/combined-sale-listings?${query}`);
}

async function getDeals(params = {}) {
  const query = new URLSearchParams(params).toString();
  return api(`/deals?${query}`);
}

async function getOpportunities(params = {}) {
  const query = new URLSearchParams(params).toString();
  return api(`/opportunities?${query}`);
}

// Plans
async function getPlans(params = {}) {
  const query = new URLSearchParams(params).toString();
  return api(`/plans?${query}`);
}

async function getPlanDetail(mpId) {
  return api(`/plan/${encodeURIComponent(mpId)}`);
}

// Map
async function getParcelPolygon(gush, helka) {
  return api(`/parcel-polygon?gush=${encodeURIComponent(gush)}&helka=${encodeURIComponent(helka)}`);
}

// Storage
function storeSearchData(data) {
  localStorage.setItem('nadlanfix_search', JSON.stringify(data));
}

function getSearchData() {
  const data = localStorage.getItem('nadlanfix_search');
  return data ? JSON.parse(data) : null;
}

function clearSearchData() {
  localStorage.removeItem('nadlanfix_search');
}
