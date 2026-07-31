export interface FrameMetrics {
  meanLuma: number;
  medianLuma: number;
  darkPixelRatio: number;
  sharpness: number;
  grayscale: Uint8Array;
  perceptualHash: bigint;
}

export function computeFrameMetrics(
  imageData: ImageData,
  darkPixelThreshold: number,
): FrameMetrics {
  const { data, width, height } = imageData;
  const pixelCount = width * height;
  const grayscale = new Uint8Array(pixelCount);
  const histogram = new Uint32Array(256);
  let sum = 0;
  let darkCount = 0;
  for (let pixel = 0; pixel < pixelCount; pixel += 1) {
    const offset = pixel * 4;
    const luma = Math.round(
      data[offset] * 0.2126 +
        data[offset + 1] * 0.7152 +
        data[offset + 2] * 0.0722,
    );
    grayscale[pixel] = luma;
    histogram[luma] += 1;
    sum += luma;
    if (luma <= darkPixelThreshold) {
      darkCount += 1;
    }
  }

  let accumulated = 0;
  let lowerMedian = 0;
  let upperMedian = 0;
  let lowerMedianFound = false;
  const lowerTarget = Math.floor((pixelCount - 1) / 2);
  const upperTarget = Math.floor(pixelCount / 2);
  for (let value = 0; value < histogram.length; value += 1) {
    accumulated += histogram[value];
    if (accumulated > lowerTarget && !lowerMedianFound) {
      lowerMedian = value;
      lowerMedianFound = true;
    }
    if (accumulated > upperTarget) {
      upperMedian = value;
      break;
    }
  }
  const median = (lowerMedian + upperMedian) / 2;

  const laplacians: number[] = [];
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const index = y * width + x;
      laplacians.push(
        grayscale[index - 1] +
          grayscale[index + 1] +
          grayscale[index - width] +
          grayscale[index + width] -
          4 * grayscale[index],
      );
    }
  }
  const laplacianMean =
    laplacians.reduce((total, value) => total + value, 0) /
    Math.max(1, laplacians.length);
  const sharpness =
    laplacians.reduce(
      (total, value) => total + (value - laplacianMean) ** 2,
      0,
    ) / Math.max(1, laplacians.length);

  const cells: number[] = [];
  for (let cellY = 0; cellY < 8; cellY += 1) {
    for (let cellX = 0; cellX < 8; cellX += 1) {
      const x = Math.min(width - 1, Math.floor(((cellX + 0.5) * width) / 8));
      const y = Math.min(height - 1, Math.floor(((cellY + 0.5) * height) / 8));
      cells.push(grayscale[y * width + x]);
    }
  }
  const cellMean =
    cells.reduce((total, value) => total + value, 0) / cells.length;
  let perceptualHash = 0n;
  cells.forEach((value, index) => {
    if (value >= cellMean) {
      perceptualHash |= 1n << BigInt(index);
    }
  });

  return {
    meanLuma: sum / Math.max(1, pixelCount),
    medianLuma: median,
    darkPixelRatio: darkCount / Math.max(1, pixelCount),
    sharpness,
    grayscale,
    perceptualHash,
  };
}

export function meanAbsoluteDifference(
  current: Uint8Array,
  previous?: Uint8Array,
): number {
  if (!previous || current.length !== previous.length) {
    return 255;
  }
  let sum = 0;
  for (let index = 0; index < current.length; index += 1) {
    sum += Math.abs(current[index] - previous[index]);
  }
  return sum / Math.max(1, current.length);
}

export function hammingDistance(left: bigint, right?: bigint): number {
  if (right === undefined) {
    return 64;
  }
  let difference = left ^ right;
  let count = 0;
  while (difference !== 0n) {
    count += Number(difference & 1n);
    difference >>= 1n;
  }
  return count;
}
