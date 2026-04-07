import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { COLORS } from '../theme';

export default function Badge({ label, color = COLORS.blue, small = false }) {
  return (
    <View style={[styles.badge, { backgroundColor: color + '22', borderColor: color + '44' }, small && styles.small]}>
      <Text style={[styles.text, { color }, small && styles.smallText]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 20,
    borderWidth: 1,
    alignSelf: 'flex-start',
  },
  small: { paddingHorizontal: 6, paddingVertical: 2 },
  text: { fontSize: 12, fontWeight: '600' },
  smallText: { fontSize: 10 },
});
