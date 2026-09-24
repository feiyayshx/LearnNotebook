#!/usr/bin/env node
/**
 * spec-lint.mjs — spec-writing skill 的可执行质量钩子
 *
 * 用法: node scripts/spec-lint.mjs <spec文件路径>
 * 退出码: 0 = 通过（可含警告）, 1 = 存在错误项, 2 = 用法/读取错误
 *
 * 检查对象：按 template.md 生成的 Spec 文件。勿对 template.md 本身运行（模板含占位符属正常）。
 * 词表与 clarification.md 第一节保持一致，修改任一侧需同步另一侧。
 */
import { readFileSync } from 'node:fs';
import { basename } from 'node:path';

const file = process.argv[2];
if (!file) {
  console.error('用法: node scripts/spec-lint.mjs <spec文件路径>');
  process.exit(2);
}

let text;
try {
  text = readFileSync(file, 'utf8');
} catch (e) {
  console.error(`无法读取文件: ${file} (${e.message})`);
  process.exit(2);
}
const lines = text.split(/\r?\n/);

const errors = [];
const warnings = [];

// ---------- 1. 模糊词扫描（警告级；单字"等"因误报率高不收录，用"等等/等格式"等词形覆盖） ----------
const VAGUE_WORDS = [
  '等等', '之类', '尽量', '尽可能', '较快', '合理', '合适', '友好', '灵活',
  '强大', '稳定', '高效', '若干', '偶尔', '一般', '通常', '大概', '差不多',
  '等格式', '响应迅速', '操作简单',
  'etc.', 'and so on', 'appropriate', 'user-friendly', 'flexible', 'robust',
  'efficient', 'several', 'various', 'usually', 'generally', 'approximately',
  'as soon as possible',
];
lines.forEach((line, i) => {
  for (const w of VAGUE_WORDS) {
    if (line.toLowerCase().includes(w.toLowerCase())) {
      warnings.push(`[模糊词] L${i + 1}: 命中 "${w}" -> ${line.trim().slice(0, 60)}`);
    }
  }
});

// ---------- 2. 需求编号唯一性 + 验收标准存在性（错误级） ----------
const reqBlocks = [];
let current = null;
lines.forEach((line, i) => {
  const m = line.match(/^###\s+(FR|NFR)-(\d+)/);
  if (m) {
    if (current) reqBlocks.push(current);
    current = { id: `${m[1]}-${m[2]}`, line: i + 1, body: [] };
  } else if (current) {
    if (/^#{1,3}\s/.test(line)) {
      reqBlocks.push(current);
      current = null;
    } else {
      current.body.push(line);
    }
  }
});
if (current) reqBlocks.push(current);

const seen = new Map();
for (const b of reqBlocks) {
  if (seen.has(b.id)) {
    errors.push(`[编号重复] ${b.id} 同时出现在 L${seen.get(b.id)} 与 L${b.line}`);
  } else {
    seen.set(b.id, b.line);
  }
  if (!b.body.join('\n').includes('验收标准')) {
    errors.push(`[验收标准缺失] ${b.id} (L${b.line}) 块内无"验收标准"`);
  }
}

// ---------- 3. 【待确认 Q-x】引用与 Open Questions 表一致性 ----------
const refSet = new Set([...text.matchAll(/【待确认\s*(Q-\d+)】/g)].map((m) => m[1]));
const tableSet = new Set([...text.matchAll(/^\|\s*(Q-\d+)\s*\|/gm)].map((m) => m[1]));
for (const q of refSet) {
  if (!tableSet.has(q)) errors.push(`[孤儿引用] 正文【待确认 ${q}】在 Open Questions 表中无对应条目`);
}
for (const q of tableSet) {
  if (!refSet.has(q)) warnings.push(`[孤儿条目] Open Questions 表 ${q} 在正文无对应【待确认 ${q}】引用`);
}

// ---------- 4. 【假设】【推测】标记残留（警告级，定稿前须清零） ----------
const assumeCount = (text.match(/【假设】/g) || []).length;
const guessCount = (text.match(/【推测】/g) || []).length;
if (assumeCount > 0) warnings.push(`[标记残留] 【假设】x ${assumeCount}，定稿前须清零或经用户书面接受`);
if (guessCount > 0) warnings.push(`[标记残留] 【推测】x ${guessCount}，确认后应改标【已验证】或删除`);

// ---------- 5. 模板占位符残留（错误级） ----------
lines.forEach((line, i) => {
  const m = line.match(/\{[^{}]{1,40}\}/);
  if (m) errors.push(`[占位符残留] L${i + 1}: "${m[0]}"`);
});

// ---------- 汇总报告 ----------
const frCount = reqBlocks.filter((b) => b.id.startsWith('FR')).length;
const nfrCount = reqBlocks.filter((b) => b.id.startsWith('NFR')).length;
console.log(`\n=== spec-lint 报告: ${basename(file)} ===`);
console.log(`需求条目: FR ${frCount} / NFR ${nfrCount} | Open Questions: 引用 ${refSet.size} / 表条目 ${tableSet.size}`);
if (errors.length > 0) {
  console.log(`\n错误 (${errors.length}):`);
  errors.forEach((e) => console.log(`  [E] ${e}`));
}
if (warnings.length > 0) {
  console.log(`\n警告 (${warnings.length}):`);
  warnings.forEach((w) => console.log(`  [W] ${w}`));
}
if (errors.length === 0 && warnings.length === 0) console.log('全部通过，无错误无警告。');
console.log(errors.length > 0 ? '\n结果: FAIL' : '\n结果: PASS');
process.exit(errors.length > 0 ? 1 : 0);
