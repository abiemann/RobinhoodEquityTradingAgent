/*
 * RHMRA QR encoder (byte mode, error correction level M).
 * Derived from the QR Code specification; no network service or dependency is used.
 * Copyright (c) 2026 RHMRA contributors. MIT licensed.
 */
(function (root) {
  "use strict";

  const BLOCKS_M = [
    null,
    [[1, 26, 16]],
    [[1, 44, 28]],
    [[1, 70, 44]],
    [[2, 50, 32]],
    [[2, 67, 43]],
    [[4, 43, 27]],
    [[4, 49, 31]],
    [[2, 60, 38], [2, 61, 39]],
    [[3, 58, 36], [2, 59, 37]],
    [[4, 69, 43], [1, 70, 44]],
  ];
  const ALIGNMENT = [
    null, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
    [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50],
  ];
  const GF_EXP = new Uint8Array(512);
  const GF_LOG = new Uint8Array(256);
  let value = 1;
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = value;
    GF_LOG[value] = i;
    value <<= 1;
    if (value & 0x100) value ^= 0x11d;
  }
  for (let i = 255; i < GF_EXP.length; i++) GF_EXP[i] = GF_EXP[i - 255];

  function multiply(a, b) {
    return a && b ? GF_EXP[GF_LOG[a] + GF_LOG[b]] : 0;
  }

  function polynomialMultiply(a, b) {
    const result = new Uint8Array(a.length + b.length - 1);
    for (let i = 0; i < a.length; i++) {
      for (let j = 0; j < b.length; j++) result[i + j] ^= multiply(a[i], b[j]);
    }
    return Array.from(result);
  }

  function errorCorrection(data, length) {
    let generator = [1];
    for (let i = 0; i < length; i++) generator = polynomialMultiply(generator, [1, GF_EXP[i]]);
    const work = Uint8Array.from([...data, ...new Array(length).fill(0)]);
    for (let i = 0; i < data.length; i++) {
      const factor = work[i];
      if (!factor) continue;
      for (let j = 0; j < generator.length; j++) work[i + j] ^= multiply(generator[j], factor);
    }
    return Array.from(work.slice(data.length));
  }

  function appendBits(bits, value, length) {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  }

  function dataCodewords(text) {
    const bytes = Array.from(new TextEncoder().encode(text));
    for (let version = 1; version < BLOCKS_M.length; version++) {
      const blocks = BLOCKS_M[version];
      const capacity = blocks.reduce((sum, [count, , data]) => sum + count * data, 0);
      const countBits = version < 10 ? 8 : 16;
      if (4 + countBits + bytes.length * 8 > capacity * 8) continue;
      const bits = [];
      appendBits(bits, 0b0100, 4);
      appendBits(bits, bytes.length, countBits);
      for (const byte of bytes) appendBits(bits, byte, 8);
      for (let i = 0, n = Math.min(4, capacity * 8 - bits.length); i < n; i++) bits.push(0);
      while (bits.length % 8) bits.push(0);
      const words = [];
      for (let i = 0; i < bits.length; i += 8) {
        let word = 0;
        for (let j = 0; j < 8; j++) word = (word << 1) | bits[i + j];
        words.push(word);
      }
      for (let pad = 0; words.length < capacity; pad++) words.push(pad % 2 ? 0x11 : 0xec);
      return { version, words, blocks };
    }
    throw new Error("Share URL is too long for the local QR encoder.");
  }

  function interleave(data, blockGroups) {
    const dataBlocks = [], eccBlocks = [];
    let offset = 0;
    for (const [count, total, dataLength] of blockGroups) {
      for (let i = 0; i < count; i++) {
        const block = data.slice(offset, offset + dataLength);
        offset += dataLength;
        dataBlocks.push(block);
        eccBlocks.push(errorCorrection(block, total - dataLength));
      }
    }
    const result = [];
    const maxData = Math.max(...dataBlocks.map(block => block.length));
    const maxEcc = Math.max(...eccBlocks.map(block => block.length));
    for (let i = 0; i < maxData; i++) {
      for (const block of dataBlocks) if (i < block.length) result.push(block[i]);
    }
    for (let i = 0; i < maxEcc; i++) {
      for (const block of eccBlocks) if (i < block.length) result.push(block[i]);
    }
    return result;
  }

  function finder(modules, row, col) {
    const size = modules.length;
    for (let y = -1; y <= 7; y++) {
      for (let x = -1; x <= 7; x++) {
        if (row + y < 0 || row + y >= size || col + x < 0 || col + x >= size) continue;
        modules[row + y][col + x] =
          (y >= 0 && y <= 6 && (x === 0 || x === 6)) ||
          (x >= 0 && x <= 6 && (y === 0 || y === 6)) ||
          (x >= 2 && x <= 4 && y >= 2 && y <= 4);
      }
    }
  }

  function bch(value, polynomial) {
    let shifted = value;
    let degree = 0;
    for (let p = polynomial; p; p >>>= 1) degree++;
    shifted <<= degree - 1;
    while (true) {
      let shiftedDegree = 0;
      for (let p = shifted; p; p >>>= 1) shiftedDegree++;
      if (shiftedDegree < degree) break;
      shifted ^= polynomial << (shiftedDegree - degree);
    }
    return (value << (degree - 1)) | shifted;
  }

  function maskBit(mask, row, col) {
    switch (mask) {
      case 0: return (row + col) % 2 === 0;
      case 1: return row % 2 === 0;
      case 2: return col % 3 === 0;
      case 3: return (row + col) % 3 === 0;
      case 4: return (Math.floor(row / 2) + Math.floor(col / 3)) % 2 === 0;
      case 5: return (row * col) % 2 + (row * col) % 3 === 0;
      case 6: return ((row * col) % 2 + (row * col) % 3) % 2 === 0;
      case 7: return ((row * col) % 3 + (row + col) % 2) % 2 === 0;
      default: throw new Error("Invalid QR mask.");
    }
  }

  function drawFormat(modules, mask, test) {
    // Error correction M is encoded as 00.
    const bits = bch(mask, 0x537) ^ 0x5412;
    const size = modules.length;
    for (let i = 0; i < 15; i++) {
      const dark = !test && ((bits >>> i) & 1) === 1;
      if (i < 6) modules[i][8] = dark;
      else if (i < 8) modules[i + 1][8] = dark;
      else modules[size - 15 + i][8] = dark;
      if (i < 8) modules[8][size - i - 1] = dark;
      else if (i === 8) modules[8][7] = dark;
      else modules[8][15 - i - 1] = dark;
    }
    modules[size - 8][8] = !test;
  }

  function drawVersion(modules, version, test) {
    if (version < 7) return;
    const bits = bch(version, 0x1f25);
    const size = modules.length;
    for (let i = 0; i < 18; i++) {
      const dark = !test && ((bits >>> i) & 1) === 1;
      modules[Math.floor(i / 3)][i % 3 + size - 11] = dark;
      modules[i % 3 + size - 11][Math.floor(i / 3)] = dark;
    }
  }

  function baseMatrix(version, mask, test) {
    const size = version * 4 + 17;
    const modules = Array.from({ length: size }, () => Array(size).fill(null));
    finder(modules, 0, 0);
    finder(modules, size - 7, 0);
    finder(modules, 0, size - 7);
    for (const row of ALIGNMENT[version]) {
      for (const col of ALIGNMENT[version]) {
        if (modules[row][col] !== null) continue;
        for (let y = -2; y <= 2; y++) {
          for (let x = -2; x <= 2; x++) {
            modules[row + y][col + x] = Math.max(Math.abs(x), Math.abs(y)) !== 1;
          }
        }
      }
    }
    for (let i = 8; i < size - 8; i++) {
      if (modules[i][6] === null) modules[i][6] = i % 2 === 0;
      if (modules[6][i] === null) modules[6][i] = i % 2 === 0;
    }
    drawFormat(modules, mask, test);
    drawVersion(modules, version, test);
    return modules;
  }

  function placeData(modules, bytes, mask) {
    const size = modules.length;
    let bit = 0, upward = true;
    for (let right = size - 1; right > 0; right -= 2) {
      if (right === 6) right--;
      for (let vertical = 0; vertical < size; vertical++) {
        const row = upward ? size - 1 - vertical : vertical;
        for (let offset = 0; offset < 2; offset++) {
          const col = right - offset;
          if (modules[row][col] !== null) continue;
          let dark = bit < bytes.length * 8 && ((bytes[bit >>> 3] >>> (7 - (bit & 7))) & 1) === 1;
          if (maskBit(mask, row, col)) dark = !dark;
          modules[row][col] = dark;
          bit++;
        }
      }
      upward = !upward;
    }
  }

  function penalty(modules) {
    const size = modules.length;
    let score = 0, dark = 0;
    const linePenalty = line => {
      let points = 0, run = 1;
      for (let i = 1; i < line.length; i++) {
        if (line[i] === line[i - 1]) run++;
        else { if (run >= 5) points += 3 + run - 5; run = 1; }
      }
      if (run >= 5) points += 3 + run - 5;
      for (let i = 0; i + 6 < line.length; i++) {
        if (line.slice(i, i + 7).map(Number).join("") !== "1011101") continue;
        const before = i >= 4 && line.slice(i - 4, i).every(value => !value);
        const after = i + 11 <= line.length && line.slice(i + 7, i + 11).every(value => !value);
        if (before || after) points += 40;
      }
      return points;
    };
    for (let row = 0; row < size; row++) {
      score += linePenalty(modules[row]);
      dark += modules[row].filter(Boolean).length;
    }
    for (let col = 0; col < size; col++) score += linePenalty(modules.map(row => row[col]));
    for (let row = 0; row < size - 1; row++) {
      for (let col = 0; col < size - 1; col++) {
        const value = modules[row][col];
        if (modules[row + 1][col] === value && modules[row][col + 1] === value && modules[row + 1][col + 1] === value) score += 3;
      }
    }
    score += Math.floor(Math.abs(dark * 20 - size * size * 10) / (size * size)) * 10;
    return score;
  }

  function matrix(text) {
    const encoded = dataCodewords(text);
    const bytes = interleave(encoded.words, encoded.blocks);
    let best = null, bestScore = Infinity;
    for (let mask = 0; mask < 8; mask++) {
      const modules = baseMatrix(encoded.version, mask, true);
      placeData(modules, bytes, mask);
      const score = penalty(modules);
      if (score < bestScore) { best = mask; bestScore = score; }
    }
    const modules = baseMatrix(encoded.version, best, false);
    placeData(modules, bytes, best);
    return modules;
  }

  function render(element, text) {
    const modules = matrix(text);
    const quiet = 4, size = modules.length + quiet * 2;
    let path = "";
    for (let row = 0; row < modules.length; row++) {
      for (let col = 0; col < modules.length; col++) {
        if (modules[row][col]) path += `M${col + quiet} ${row + quiet}h1v1h-1z`;
      }
    }
    element.innerHTML = `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="QR code for the temporary phone dashboard"><rect width="${size}" height="${size}" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
  }

  root.RhmraQr = Object.freeze({ matrix, render });
})(globalThis);
