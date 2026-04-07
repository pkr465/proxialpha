import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { SIGNAL_COLOR, COLORS } from '../theme';

const LABEL_MAP = {
  STRONG_BUY: 'STRONG BUY',
  BUY: 'BUY',
  HOLD: 'HOLD',
  SELL: 'SELL',
  STRONG_SELL: 'STRONG SELL',
};

export default function SignalBadge({ signal }) {
  const key = (signal || 'HOLD').toUpperCase().replace(' ', '_');
  const color = SIGNAL_COLOR[key] || COLORS.textMuted;
  const label = LABEL_MAP[key] || signal;
  return (
    <View style={[styles.badge, { backgroundColor: color + '22', borderColor: color + '55' }]}>
      <View style={[styles.dot, { backgroundColor: color }]} />
      <Text style={[styles.text, { color }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 12,
    borderWidth: 1,
    alignSelf: 'flex-start',
    gap: 4,
  },
  dot: { width: 6, height: 6, borderRadius: 3 },
  text: { fontSize: 10, fontWeight: '700', letterSpacing: 0.5 },
});
