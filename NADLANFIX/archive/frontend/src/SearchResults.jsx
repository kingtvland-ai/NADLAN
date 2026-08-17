import { useEffect, useMemo, useState } from 'react';
import './App.css';

const REGIONS = [
  ['all', 'כל הארץ'], ['tel-aviv-area', 'תל אביב והסביבה'], ['center-and-sharon', 'מרכז והשרון'],
  ['south', 'דרום'], ['north-and-valleys', 'צפון והעמקים'], ['jerusalem-area', 'ירושלים והסביבה'],
  ['haifa-and-creatures', 'חיפה והקריות'],
];

const CATEGORIES = [
  { id: 'listings', icon: '⌂', title: 'נכסים בשוק', subtitle: 'מודעות מכירה, שכירות ומסחרי' },
  { id: 'planning', icon: '▦', title: 'תכנון והשבחה', subtitle: 'תכניות, יחידות דיור והתחדשות' },
  { id: 'parcel', icon: '⌖', title: 'בדיקת גוש וחלקה', subtitle: 'חיפוש ממוקד במאגר PlanWatch' },
  { id: 'sources', icon: '◎', title: 'מקורות מידע', subtitle: 'סטטוס API וחיבורים ארגוניים' },
];

const STATUS_LABELS = {
  connected: 'מחובר', partial: 'חיבור חלקי', 'key-required': 'דרוש מפתח', 'key-ready': 'מפתח זמין',
  'partner-key-required': 'דרוש הסכם', unverified: 'טרם אומת',
};

async function getJson(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `הבקשה נכשלה (${response.status})`);
  return body;
}

export default function SearchResults() {
  const [category, setCategory] = useState('listings');
  const [compact, setCompact] = useState(true);
  const [advanced, setAdvanced] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [data, setData] = useState(null);
  const [regionSlug, setRegionSlug] = useState('tel-aviv-area');
  const [dealType, setDealType] = useState('forsale');
  const [source, setSource] = useState('yad2');
  const [area, setArea] = useState('');
  const [city, setCity] = useState('');
  const [fetchAll, setFetchAll] = useState(false);
  const [page, setPage] = useState(1);
  const [gush, setGush] = useState('');
  const [helka, setHelka] = useState('');
  const [brokerageFilter, setBrokerageFilter] = useState('כל הנכסים');
  const [lightbox, setLightbox] = useState(null);

  const selectedCategory = useMemo(() => CATEGORIES.find((item) => item.id === category), [category]);

  function selectCategory(nextCategory) {
    setCategory(nextCategory);
    setData(null);
    setError('');
    setAdvanced(false);
  }

  async function run(action) {
    setLoading(true); setError('');
    try { setData(await action()); } catch (err) { setError(err.message); } finally { setLoading(false); }
  }

  function searchListings(targetPage = 1) {
    setPage(targetPage);
    const params = new URLSearchParams({ regionSlug, dealType });
    if (targetPage > 1) params.set('page', String(targetPage));
    if (area) params.set('area', area);
    if (city) params.set('city', city);
    if (fetchAll) params.set('all', '1');

    // For local APIs (yad2-8099 / onmap), increase target to 2000 to get more listings
    const isLocalSource = source === 'local' || source === 'onmap';
    const limit = isLocalSource ? '2000' : '400';
    const url = isLocalSource
      ? `/api/local-listings?${new URLSearchParams({ city, area, limit, source })}`
      : `/api/listings?${params}`;
    run(() => getJson(url));
  }

  function loadPlanning() { run(() => getJson('/api/planwatch/bootstrap')); }
  function loadSources() { run(() => getJson('/api/integrations')); }
  function searchParcel() {
    if (!gush || !helka) { setError('יש להזין גם גוש וגם חלקה.'); return; }
    run(() => getJson(`/api/planwatch/parcel?gush=${encodeURIComponent(gush)}&helka=${encodeURIComponent(helka)}`));
  }

  return (
    <main className={`app ${compact ? 'compact' : ''}`}>
      <header className="hero">
        <div className="hero-inner">
          <div><span className="eyebrow">מרכז מודיעין נדל״ן</span><h1>כל המידע. בלי הרעש.</h1><p>בחרו מה מעניין אתכם והמערכת תציג רק את הכלים הרלוונטיים.</p></div>
          <button className="view-toggle" onClick={() => setCompact(!compact)} aria-pressed={compact}>
            {compact ? 'מצב מפורט' : 'מצב נקי'}
          </button>
        </div>
      </header>

      <section className="workspace" aria-label="מרכז חיפוש נדל״ן">
        <nav className="category-grid" aria-label="קטגוריות מידע">
          {CATEGORIES.map((item) => (
            <button key={item.id} className={`category-card ${category === item.id ? 'active' : ''}`} onClick={() => selectCategory(item.id)}>
              <span className="category-icon">{item.icon}</span><span><strong>{item.title}</strong>{!compact && <small>{item.subtitle}</small>}</span>
            </button>
          ))}
        </nav>

        <section className="panel">
          <div className="panel-heading"><div><span className="panel-kicker">{selectedCategory.icon} {selectedCategory.title}</span><h2>{selectedCategory.subtitle}</h2></div></div>

          {category === 'listings' && (
            <div className="filters">
              <label><span>מקור נתונים</span><select value={source} onChange={(e) => setSource(e.target.value)}><option value="yad2">Yad2 מחובר</option><option value="local">Yad2 מקומי 8099</option><option value="onmap">על המפה</option></select></label>
              <label><span>אזור</span><select value={regionSlug} onChange={(e) => setRegionSlug(e.target.value)}>{REGIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label><span>סוג עסקה</span><select value={dealType} onChange={(e) => setDealType(e.target.value)}><option value="forsale">מכירה</option><option value="rent">השכרה</option><option value="commercial">מסחרי</option></select></label>
              <button className="primary" onClick={() => searchListings(1)} disabled={loading}>{loading ? 'מחפש…' : 'הצג נכסים'}</button>
              <button className="secondary" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}>{advanced ? 'פחות מסננים' : 'מסננים נוספים'}</button>
              {source === 'local' && <p className="source-hint">המקור המקומי יאסוף עד 2000 נכסי מכירה ייחודיים מ־<code>/api/yad2</code>, כולל מודעות פרטיות.</p>}
              {source === 'onmap' && <p className="source-hint">מקור "על המפה" — נכסים מ־<code>/api/onmap</code> (על מפה.co.il).</p>}
              {advanced && <div className="advanced-filters"><label><span>קוד אזור משנה</span><input inputMode="numeric" value={area} onChange={(e) => setArea(e.target.value)} placeholder="לדוגמה 1" /></label><label><span>קוד עיר</span><input inputMode="numeric" value={city} onChange={(e) => setCity(e.target.value)} placeholder="לדוגמה 5000" /></label><label className="check"><input type="checkbox" checked={fetchAll} onChange={(e) => setFetchAll(e.target.checked)} /> משיכת כל העמודים</label></div>}
            </div>
          )}

          {category === 'planning' && <div className="action-intro"><p>תמונת מצב מרוכזת של מאגרי התכנון המקומיים.</p><button className="primary" onClick={loadPlanning} disabled={loading}>טען תמונת תכנון</button></div>}
          {category === 'parcel' && <div className="filters parcel-filters"><label><span>גוש</span><input inputMode="numeric" value={gush} onChange={(e) => setGush(e.target.value)} /></label><label><span>חלקה</span><input inputMode="numeric" value={helka} onChange={(e) => setHelka(e.target.value)} /></label><button className="primary" onClick={searchParcel} disabled={loading}>בדוק חלקה</button></div>}
          {category === 'sources' && <div className="action-intro"><p>בדקו מה מחובר, מה דורש מפתח ומה ממתין להסכם מסחרי.</p><button className="primary" onClick={loadSources} disabled={loading}>בדוק חיבורים</button></div>}

          {error && <div className="notice error" role="alert"><strong>לא הצלחנו להשלים את הבקשה.</strong><span>{error}</span></div>}
          {loading && <div className="loading"><span className="spinner" /> טוען נתונים…</div>}
          {!loading && data && <Results category={category} data={data} page={page} onPage={searchListings} compact={compact} brokerageFilter={brokerageFilter} onBrokerageChange={setBrokerageFilter} onImageClick={(item) => setLightbox({ listing: item, index: 0 })} />}
        </section>
      </section>
      {lightbox && <Lightbox data={lightbox} onClose={() => setLightbox(null)} />}
    </main>
  );
}

function Results({ category, data, page, onPage, compact, brokerageFilter, onBrokerageChange, onImageClick }) {
  if (category === 'sources') return <div className="source-grid">{data.integrations?.map((source) => <article className="source-card" key={source.id}><div><h3>{source.name}</h3><span className={`status ${source.status}`}>{STATUS_LABELS[source.status] || source.status}</span></div><p>{source.description}</p><small>{source.access}</small></article>)}</div>;
  if (category === 'planning') return <div className="metric-grid"><Metric label="תכניות" value={data.stats?.plans} /><Metric label="יחידות דיור" value={data.housing_units} /><Metric label="רשויות" value={data.jurisdictions?.length} /><Metric label="מחוזות" value={data.counties?.length} /></div>;
  if (category === 'parcel') return <pre className="data-preview">{JSON.stringify(data, null, 2)}</pre>;
  const listings = data.listings || [];
  const hasPagination = !data.allPages && !data.allRegions && data.totalPages != null;
  const isPrivateListing = (item) => item.isPrivate != null ? item.isPrivate : !item.agency;
  const filteredListings = brokerageFilter === 'ללא תיווך' ? listings.filter(isPrivateListing)
    : brokerageFilter === 'עם תיווך' ? listings.filter((item) => !isPrivateListing(item)) : listings;
  const sourceLabel = data.source === 'onmap' ? 'על המפה'
    : data.source === 'yad2-local-api' ? 'Yad2 מקומי 8099' : 'Yad2';
  //: הודעת drift מגיעה מהמקור (8099) כשהאתר השתנה או לא חולצו רשומות —
  //: מציגים אותה בולטת כדי שהמשתמש לא יחשוב שהרשימה ריקה באמת.
  const driftNote = data.driftNote ? data.driftNote : '';
  return <><div className="result-head"><strong>{data.totalCount ?? listings.length} תוצאות</strong><span>{sourceLabel}</span></div>{data.sourceWarning && <div className="notice warning">{data.sourceWarning}</div>}{driftNote && <div className="notice warning">{driftNote}</div>}<div className="brokerage-filter">{['כל הנכסים', 'ללא תיווך', 'עם תיווך'].map((label) => (<button key={label} className={`brokerage-btn ${brokerageFilter === label ? 'active' : ''}`} onClick={() => onBrokerageChange(label)}>{label}</button>))}</div><div className={`listing-grid ${compact ? 'dense' : ''}`}>{filteredListings.map((item) => <a className="listing-card" href={item.url || '#'} target="_blank" rel="noreferrer" key={item.id || item.url}>{item.image && <img src={item.image} alt="" loading="lazy" onClick={(e) => { e.preventDefault(); e.stopPropagation(); onImageClick?.(item); }} />}<div><strong className="price">{item.price ? `₪${Number(item.price).toLocaleString('he-IL')}` : 'מחיר לא צוין'}</strong><h3>{item.title || 'נכס ללא כותרת'}</h3><p>{[item.city, item.neighborhood].filter(Boolean).join(' · ')}</p>{!compact && <small>{[item.rooms && `${item.rooms} חדרים`, item.squareMeters && `${item.squareMeters} מ״ר`].filter(Boolean).join(' · ')}</small>}</div></a>)}</div>{filteredListings.length === 0 && <div className="empty">לא נמצאו תוצאות במסננים שנבחרו.</div>} {hasPagination && <div className="pagination"><button disabled={page <= 1} onClick={() => onPage(page - 1)}>הקודם</button><span>עמוד {page}</span><button disabled={page >= data.totalPages} onClick={() => onPage(page + 1)}>הבא</button></div>}</>;
}

function Lightbox({ data, onClose }) {
  const { listing, index } = data;
  const images = (listing.images && listing.images.length ? listing.images : [listing.image]).filter(Boolean);
  const [current, setCurrent] = useState(Math.min(index || 0, Math.max(images.length - 1, 0)));

  useEffect(() => {
    if (!images.length) return;
    const handler = (e) => {
      if (e.key === 'Escape') onClose();
      // RTL: forward (next) moves left, backward (previous) moves right.
      if (e.key === 'ArrowLeft') setCurrent((c) => (c + 1) % images.length);
      if (e.key === 'ArrowRight') setCurrent((c) => (c - 1 + images.length) % images.length);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [images.length, onClose]);

  const prev = () => setCurrent((c) => (c - 1 + images.length) % images.length);
  const next = () => setCurrent((c) => (c + 1) % images.length);

  return (
    <div className="lightbox-overlay" onClick={onClose} role="dialog" aria-modal="true">
      <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
        <button className="lightbox-close" onClick={onClose} aria-label="סגירה">✕</button>
        {images.length > 1 && <button className="lightbox-nav prev" onClick={prev} aria-label="הקודם">›</button>}
        <img src={images[current]} alt="" />
        {images.length > 1 && <button className="lightbox-nav next" onClick={next} aria-label="הבא">‹</button>}
        {images.length > 1 && <div className="lightbox-counter">{current + 1} / {images.length}</div>}
      </div>
    </div>
  );
}

function Metric({ label, value }) { return <article className="metric"><span>{label}</span><strong>{Number(value || 0).toLocaleString('he-IL')}</strong></article>; }