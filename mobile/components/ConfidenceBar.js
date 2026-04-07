import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { COLORS } from '../theme';

export default function ConfidenceBar({ value = 0, width = 80, height = 6 }) {
  const pct = Math.min(Math.max(value, 0), 1);
  const color = pct >= 0.7 ? COLORS.green : pct >= 0.4 ? COLORS.amber : COLORS.red;
  return (
    <View style={styles.row}>
      <View style={[styles.track, { width, height }]}>
        <View style={[styles.fill, { width: `${pct * 100}%`, height, backgroundColor: color }]} />
      </View>
      <Text style={[styles.label, { color }]}>{(pct * 100).toFixed(0)}%</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  track: { backgroundColor: COLORS.bgInput, borderRadius: 3, overflow: 'hidden' },
  fill: { borderRadius: 3 },
  label: { fontSize: 11, fontWeight: '600', minWidth: 32 },
});
