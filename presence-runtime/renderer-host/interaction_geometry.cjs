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
    return {
      x: Math.round(startBounds.x),
      y: Math.round(startBounds.y),
      width: Math.max(160, Math.round(startBounds.width + deltaX)),
      height: Math.max(160, Math.round(startBounds.height + deltaY)),
    };
  }
  throw new TypeError("renderer interaction mode is invalid");
}

module.exports = { interactionBounds };
