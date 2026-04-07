import React from 'react';
import { View, StyleSheet } from 'react-native';
import { COLORS } from '../theme';

/**
 * Simple sparkline chart drawn with basic Views (no SVG dependency needed).
 * Renders a series of vertical bars representing data points.
 */
export default function MiniChart({ data = [], width = 80, height = 32, color = COLORS.blue }) {
  if (!data.length) return null;
  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = max - min || 1;
  const barWidth = Math.max(1, (width / data.length) - 1);

  return (
    <View style={[styles.container, { width, height }]}>
      {data.map((val, i) => {
        const barHeight = ((val - min) / range) * height;
        return (
          <View
            key={i}
            style={[styles.bar, {
              width: barWidth,
              height: Math.max(1, barHeight),
              backgroundColor: color + (i === data.length - 1 ? 'ff' : '88'),
            }]}
          />
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: 1,
  },
  bar: {
    borderRadius: 1,
  },
});
