// Эталон для сверки питоновского порта распознавателя капчи.
//
// Печатает вход сети (1280 чисел на картинку), посчитанный ОРИГИНАЛОМ
// на JS из sudtudtestbot. Выхлоп лежит в tests/fixtures/captcha_reference.json
// и сверяется тестом test_preprocessing_matches_the_original_exactly.
//
// Сеть здесь не участвует, поэтому эталон не устаревает при переобучении.
//
// Запуск (путь к проекту друга — первым аргументом):
//   node scripts/dump-captcha-reference.mjs <sudtudtestbot> <корпус> [сколько]
//     > tests/fixtures/captcha_reference.json
import fs from 'node:fs';
import path from 'node:path';
const origin = process.argv[2];
const { decodeCaptchaPng, captchaVec } = await import(path.join(origin, 'server/captcha.js'));

const dir = process.argv[3], limit = +(process.argv[4] || 60);
const files = fs.readdirSync(dir).filter((f) => f.endsWith('.png')).sort().slice(0, limit);
const out = {};
for (const f of files) {
  const img = decodeCaptchaPng(fs.readFileSync(path.join(dir, f)));
  out[f] = Array.from(captchaVec(img));
}
process.stdout.write(JSON.stringify(out));
