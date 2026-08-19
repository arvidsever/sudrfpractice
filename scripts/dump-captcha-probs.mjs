// Эталон второго уровня: вероятности, посчитанные ОРИГИНАЛОМ на JS.
//
// В отличие от эталона препроцессинга, этот зависит от весов и устаревает
// при каждом переобучении — поэтому в репозитории не хранится, а снимается
// на месте перед сверкой.
//
// Запуск:
//   CAPTCHA_MODEL=<веса> node scripts/dump-captcha-probs.mjs \
//     <sudtudtestbot> <корпус> [сколько] > /tmp/probs_js.json
import fs from 'node:fs';
import path from 'node:path';

const origin = process.argv[2];
const { decodeCaptchaPng, captchaVec, cnnProbs, loadModel } =
  await import(path.join(origin, 'server/captcha.js'));

const dir = process.argv[3], limit = +(process.argv[4] || 200);
const model = loadModel(true);
if (!model) { console.error('веса не загрузились: проверьте CAPTCHA_MODEL'); process.exit(1); }

const files = fs.readdirSync(dir).filter((f) => f.endsWith('.png')).sort().slice(0, limit);
const out = {};
for (const f of files) {
  const vec = captchaVec(decodeCaptchaPng(fs.readFileSync(path.join(dir, f))));
  out[f] = {
    tta: Array.from(cnnProbs(model, vec, true)),
    single: Array.from(cnnProbs(model, vec, false)),
  };
}
process.stdout.write(JSON.stringify(out));
