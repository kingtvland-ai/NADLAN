"""Quick smoke test for extract_listing_image and the scored snapshot."""
import sys
sys.path.insert(0, ".")
from ingestion.feeds.external_listings_view import extract_listing_image, _scoring_snapshot

ad_html = (
    '<div class="card-block" data-id="10514093"><picture>'
    '<source srcset="//img4.ad.co.il/NadlanSaleImages/1433423-300_0.jpg"/>'
    '<img src="//img4.ad.co.il/NadlanSaleImages/1433423-400_0.jpg"/>'
    '</picture></div>'
)
komo_html = (
    '<div class="View_Ad_Details"><div class="image__wrapper">'
    '<img alt="x" loading="lazy" '
    'src="/api/modaot/tmunot/showPic/list/?picNum=15517938&luachNum=2&picSize=1"/>'
    '</div></div>'
)

print("AD:", extract_listing_image(ad_html, "ad"))
print("KOMO:", extract_listing_image(komo_html, "komo"))
print("MADLAN:", extract_listing_image('{"price":"1M"}', "madlan-authorized"))
print("MADLAN-raw:", extract_listing_image(ad_html, "madlan-authorized"))

rows, sources, _, _ = _scoring_snapshot()
with_img = sum(1 for r in rows if r.get("image"))
print(f"scored rows: {len(rows)}, with image: {with_img}")
by_source = {}
for r in rows:
    key = (r.get("source"), bool(r.get("image")))
    by_source[key] = by_source.get(key, 0) + 1
print("by (source, has_image):", dict(sorted(by_source.items())))
