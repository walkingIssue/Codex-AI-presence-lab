"use strict";

function finitePoint(value) {
  return value
    && Number.isFinite(value.x)
    && Number.isFinite(value.y)
    && Math.abs(value.x) < 100000
    && Math.abs(value.y) < 100000;
}

function interactionBounds(startBounds, startPoint, currentPoint, mode) {
  if (!finitePoint(startPoint) || !finitePoint(currentPoint)) {
    throw new TypeError("renderer interaction coordinates are invalid");
  }
  const deltaX = Math.round(currentPoint.x - startPoint.x);
  const deltaY = Math.round(currentPoint.y - startPoint.y);
  if (mode === "move") {
    return {
      x: Math.round(startBounds.x + deltaX),
      y: Math.round(startBounds.y + deltaY),
      width: Math.round(startBounds.width),
      height: Math.round(startBounds.height),
    };
  }
  if (mode === "resize") {
    const width = Math.max(1, Number(startBounds.width));
    const height = Math.max(1, Number(startBounds.height));
    const horizontalScale = (width + deltaX) / width;
    const verticalScale = (height + deltaY) / height;
    const horizontalChange = Math.abs(horizontalScale - 1);
    const verticalChange = Math.abs(verticalScale - 1);
    let scale = horizontalChange >= verticalChange ? horizontalScale : verticalScale;
    const minimumScale = Math.max(160 / width, 160 / height);
    scale = Math.max(minimumScale, scale);
    return {
      x: Math.round(startBounds.x),
      y: Math.round(startBounds.y),
      width: Math.round(width * scale),
      height: Math.round(height * scale),
    };
  }
  throw new TypeError("renderer interaction mode is invalid");
}

module.exports = { interactionBounds };
