// NADLANFIX Public Site - Main Application Logic

let currentPage = 'index';
let searchData = null;

// Page Navigation
function showPage(pageId) {
  document.querySelectorAll('.page').forEach(page => {
    page.classList.remove('active');
  });

  const targetPage = document.getElementById(pageId);
  if (targetPage) {
    targetPage.classList.add('active');
    currentPage = pageId;
    window.scrollTo(0, 0);

    // Trigger page-specific initialization
    if (pageId === 'map-page') {
      initMap();
    }
  }
}

function goBack() {
  const history = getSearchData();
  if (history) {
    showPage('categories-page');
  } else {
    showPage('index-page');
  }
}

// Login
async function handleLogin(event) {
  event.preventDefault();

  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;

  if (!username || !password) {
    showError('נא למלא את כל השדות');
    return;
  }

  const btn = document.getElementById('login-btn');
  btn.disabled = true;
  btn.textContent = 'מתחבר...';

  try {
    await api.login(username, password);
    showPage('search-page');
  } catch (error) {
    showError(error.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'התחברות';
  }
}

// Search
async function handleSearch(event) {
  event.preventDefault();

  const city = document.getElementById('search-city').value;
  const street = document.getElementById('search-street').value.trim();
  const number = document.getElementById('search-number').value.trim();

  if (!city) {
    showError('נא לבחור עיר');
    return;
  }

  searchData = { city, street, number };

  try {
    // Try to resolve parcel
    if (street && number) {
      try {
        const parcel = await api.searchParcel(street, number);
        if (parcel && parcel.gush) {
          searchData.gush = parcel.gush;
          searchData.helka = parcel.helka;
        }
      } catch (e) {
        console.log('Parcel not found, continuing with text search');
      }
    }

    storeSearchData(searchData);
    showPage('categories-page');
  } catch (error) {
    showError(error.message);
  }
}

// Load localities for search
async function loadLocalities() {
  try {
    const data = await api.searchLocalities();
    const select = document.getElementById('search-city');
    if (select && data.localities) {
      data.localities.forEach(locality => {
        const option = document.createElement('option');
        option.value = locality.name;
        option.textContent = locality.name;
        select.appendChild(option);
      });
    }
  } catch (error) {
    console.error('Failed to load localities:', error);
  }
}

// Category Selection
function selectCategory(category) {
  searchData.category = category;
  storeSearchData(searchData);

  switch (category) {
    case 'transactions':
      showPage('transactions-page');
      loadTransactions();
      break;
    case 'opportunities':
      showPage('opportunities-page');
      loadOpportunities();
      break;
    case 'map':
      showPage('map-page');
      break;
    case 'planning':
      showPage('planning-page');
      loadPlanning();
      break;
  }
}

// Transactions
async function loadTransactions() {
  const container = document.getElementById('transactions-results');
  container.innerHTML = '<div class="loading"><div class="loading-spinner"></div></div>';

  try {
    const params = {
      city: searchData?.city || '',
      limit: 50,
    };

    const data = await api.getCombinedListings(params);
    renderTransactions(data);
  } catch (error) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-title">שגיאה בטעינת הנתונים</div><div>${error.message}</div></div>`;
  }
}

function renderTransactions(data) {
  const container = document.getElementById('transactions-results');

  if (!data.legacy_rows?.length && !data.live_rows?.length && !data.onmap_rows?.length && !data.facebook_rows?.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-title">לא נמצאו נכסים</div><div>נסה לשנות את הסינון</div></div>';
    return;
  }

  const allRows = [
    ...(data.legacy_rows || []),
    ...(data.live_rows || []),
    ...(data.onmap_rows || []),
    ...(data.facebook_rows || []),
  ];

  container.innerHTML = allRows.map((row, index) => `
    <div class="property-card" onclick="toggleCard(this)">
      <div class="property-card-header">
        <div>
          <div class="property-card-title">${escapeHtml(row.title || 'נכס ללא כותרת')}</div>
          <div class="property-card-subtitle">${escapeHtml(row.city || row.locality || '')} ${escapeHtml(row.neighborhood || row.address || '')}</div>
        </div>
        <div class="property-card-price">${formatPrice(row.price)}</div>
      </div>
      <div class="property-card-body">
        <div class="property-card-meta">
          ${row.rooms ? `<div class="property-card-meta-item"><strong>${row.rooms}</strong> חדרים</div>` : ''}
          ${row.sqm || row.area_m2 ? `<div class="property-card-meta-item"><strong>${row.sqm || row.area_m2}</strong> מ״ר</div>` : ''}
          ${row.floor ? `<div class="property-card-meta-item"><strong>קומה ${row.floor}</strong></div>` : ''}
          ${row.price_per_m2 ? `<div class="property-card-meta-item"><strong>${row.price_per_m2.toLocaleString()}</strong> ₪/מ״ר</div>` : ''}
        </div>
        ${row.discount_pct ? `<div class="score-badge score-high" style="margin-top: 12px;">${row.discount_pct}% הנחה מהשוק</div>` : ''}
        ${row.url ? `<a href="${escapeHtml(row.url)}" target="_blank" rel="noopener" class="btn btn-secondary" style="margin-top: 12px; padding: 8px 16px; font-size: 0.875rem;">צפה במודעה ↗</a>` : ''}
      </div>
    </div>
  `).join('');
}

// Opportunities
async function loadOpportunities() {
  const container = document.getElementById('opportunities-results');
  container.innerHTML = '<div class="loading"><div class="loading-spinner"></div></div>';

  try {
    const params = {
      city: searchData?.city || '',
      limit: 15,
      sort: 'score',
    };

    const data = await api.getDeals(params);
    renderOpportunities(data);
  } catch (error) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-title">שגיאה בטעינת הנתונים</div><div>${error.message}</div></div>`;
  }
}

function renderOpportunities(data) {
  const container = document.getElementById('opportunities-results');

  if (!data.rows?.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-title">לא נמצאו הזדמנויות</div><div>נסה לשנות את הסינון</div></div>';
    return;
  }

  const rows = data.rows.slice(0, 15);

  container.innerHTML = rows.map((row, index) => {
    const score = row.rank_score || row.deal_score || 0;
    const scoreClass = score >= 70 ? 'score-high' : score >= 40 ? 'score-medium' : 'score-low';

    return `
      <div class="property-card" onclick="toggleCard(this)">
        <div class="property-card-header">
          <div>
            <div class="property-card-title">#${index + 1} ${escapeHtml(row.title || 'נכס ללא כותרת')}</div>
            <div class="property-card-subtitle">${escapeHtml(row.city || row.locality || '')} ${escapeHtml(row.neighborhood || row.address || '')}</div>
          </div>
          <div style="text-align: left;">
            <div class="property-card-price">${formatPrice(row.price)}</div>
            <div class="score-badge ${scoreClass}" style="margin-top: 4px;">${score.toFixed(1)} נקודות</div>
          </div>
        </div>
        <div class="property-card-body">
          <div class="property-card-meta">
            ${row.rooms ? `<div class="property-card-meta-item"><strong>${row.rooms}</strong> חדרים</div>` : ''}
            ${row.sqm || row.area_m2 ? `<div class="property-card-meta-item"><strong>${row.sqm || row.area_m2}</strong> מ״ר</div>` : ''}
            ${row.floor ? `<div class="property-card-meta-item"><strong>קומה ${row.floor}</strong></div>` : ''}
            ${row.price_per_m2 ? `<div class="property-card-meta-item"><strong>${row.price_per_m2.toLocaleString()}</strong> ₪/מ״ר</div>` : ''}
          </div>
          ${row.discount_pct ? `<div style="margin-top: 12px; color: var(--success); font-weight: 600;">${row.discount_pct}% הנחה מהשוק</div>` : ''}
          ${row.benchmark_m2 ? `<div style="margin-top: 4px; color: var(--gray-600); font-size: 0.875rem;">מחיר ממוצע באזור: ${row.benchmark_m2.toLocaleString()} ₪/מ״ר</div>` : ''}
          ${row.url ? `<a href="${escapeHtml(row.url)}" target="_blank" rel="noopener" class="btn btn-secondary" style="margin-top: 12px; padding: 8px 16px; font-size: 0.875rem;">צפה במודעה ↗</a>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

// Planning
async function loadPlanning() {
  const container = document.getElementById('planning-results');
  container.innerHTML = '<div class="loading"><div class="loading-spinner"></div></div>';

  try {
    const params = {
      locality: searchData?.city || '',
      limit: 20,
    };

    const data = await api.getPlans(params);
    renderPlanning(data);
  } catch (error) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-title">שגיאה בטעינת הנתונים</div><div>${error.message}</div></div>`;
  }
}

function renderPlanning(data) {
  const container = document.getElementById('planning-results');

  if (!data.rows?.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><div class="empty-state-title">לא נמצאו תכניות</div><div>נסה לשנות את הסינון</div></div>';
    return;
  }

  container.innerHTML = data.rows.map(row => `
    <div class="property-card" onclick="toggleCard(this)">
      <div class="property-card-header">
        <div>
          <div class="property-card-title">${escapeHtml(row.pl_number || row.pl_name || 'תכנית ללא שם')}</div>
          <div class="property-card-subtitle">${escapeHtml(row.jurisdiction_name || '')} ${escapeHtml(row.station || '')}</div>
        </div>
        <div class="score-badge ${row.housing_units > 0 ? 'score-high' : 'score-medium'}">
          ${row.housing_units || 0} יח״ד
        </div>
      </div>
      <div class="property-card-body">
        <div class="property-card-meta">
          ${row.area_dunam ? `<div class="property-card-meta-item"><strong>${row.area_dunam.toLocaleString()}</strong> דונם</div>` : ''}
          ${row.landuse ? `<div class="property-card-meta-item"><strong>${escapeHtml(row.landuse)}</strong></div>` : ''}
          ${row.entity_subtype ? `<div class="property-card-meta-item"><strong>${escapeHtml(row.entity_subtype)}</strong></div>` : ''}
        </div>
        ${row.objectives ? `<div style="margin-top: 12px; color: var(--gray-700);">${escapeHtml(row.objectives)}</div>` : ''}
      </div>
    </div>
  `).join('');
}

// Map
let map = null;
let markers = [];

function initMap() {
  if (map) {
    setTimeout(() => map.invalidateSize(), 100);
    return;
  }

  map = L.map('map').setView([32.0853, 34.7818], 12);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(map);

  loadMapMarkers();
}

async function loadMapMarkers() {
  try {
    const params = {
      city: searchData?.city || '',
      limit: 100,
    };

    const data = await api.getCombinedListings(params);
    const allRows = [
      ...(data.legacy_rows || []),
      ...(data.live_rows || []),
      ...(data.onmap_rows || []),
      ...(data.facebook_rows || []),
    ];

    // Clear existing markers
    markers.forEach(marker => map.removeLayer(marker));
    markers = [];

    // Add new markers
    allRows.forEach(row => {
      if (row.lat && row.lon) {
        const marker = L.marker([row.lat, row.lon])
          .addTo(map)
          .bindPopup(`
            <div style="font-family: Heebo, sans-serif; min-width: 200px;">
              <strong>${escapeHtml(row.title || 'נכס')}</strong><br>
              <span style="color: #c9a96e; font-weight: 600;">${formatPrice(row.price)}</span><br>
              <span style="color: #666; font-size: 0.9em;">${escapeHtml(row.city || row.locality || '')}</span>
            </div>
          `);
        markers.push(marker);
      }
    });

    // Fit bounds if we have markers
    if (markers.length > 0) {
      const group = new L.featureGroup(markers);
      map.fitBounds(group.getBounds().pad(0.1));
    }
  } catch (error) {
    console.error('Failed to load map markers:', error);
  }
}

// Utility Functions
function toggleCard(card) {
  card.classList.toggle('expanded');
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatPrice(price) {
  if (!price) return 'לא צוין';
  return `${Number(price).toLocaleString()} ₪`;
}

function showError(message) {
  const errorEl = document.getElementById('error-message');
  if (errorEl) {
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
    setTimeout(() => errorEl.classList.add('hidden'), 5000);
  }
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
  // Check if user is logged in
  if (isLoggedIn() && currentPage === 'index-page') {
    showPage('search-page');
  }

  // Load localities
  loadLocalities();

  // Setup event listeners
  const loginForm = document.getElementById('login-form');
  if (loginForm) {
    loginForm.addEventListener('submit', handleLogin);
  }

  const searchForm = document.getElementById('search-form');
  if (searchForm) {
    searchForm.addEventListener('submit', handleSearch);
  }
});
