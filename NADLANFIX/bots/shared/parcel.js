// Parses free-typed גוש/חלקה text into {gush, helka}. Accepts every form
// people actually type: "גוש 6941 חלקה 23", "גוש: 6941, חלקה: 23",
// "6941/23", "6941-23", or just "6941 23" (two bare numbers, gush first -
// the conventional order). Returns null when neither number can be found,
// so the caller can ask again instead of sending a bad request to
// /api/parcel (which 400s on a missing gush/helka).
export function parseGushHelka(text) {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;

  const labeled = /גוש\D{0,5}(\d+)\D+?חלקה\D{0,5}(\d+)/.exec(trimmed);
  if (labeled) return { gush: labeled[1], helka: labeled[2] };

  const punctuated = /^(\d+)\s*[\/\-,]\s*(\d+)$/.exec(trimmed);
  if (punctuated) return { gush: punctuated[1], helka: punctuated[2] };

  const bareNumbers = trimmed.match(/\d+/g);
  if (bareNumbers && bareNumbers.length === 2) return { gush: bareNumbers[0], helka: bareNumbers[1] };

  return null;
}
