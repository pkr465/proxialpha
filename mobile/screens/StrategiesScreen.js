import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, ScrollView, StyleSheet, TouchableOpacity,
  RefreshControl, ActivityIndicator, Switch,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { COLORS, STRATEGY_COLORS } from '../theme';
import Card from '../components/Card';
import api from '../api';

const CATEGORY_ICONS = {
  Value: 'trending-down-outline',
  Trend: 'trending-up-outline',
  Technical: 'bar-chart-outline',
  Accumulation: 'layers-outline',
  Macro: 'globe-outline',
  'Short-Term': 'flash-outline',
  Arbitrage: 'swap-horizontal-outline',
  Flow: 'water-outline',
  Event: 'calendar-outline',
  Custom: 'build-outline',
  AI: 'hardware-chip-outline',
  Other: 'ellipsis-horizontal-outline',
};

export default function StrategiesScreen() {
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [strategies, setStrategies] = useState([]);
  const [toggling, setToggling] = useState(null);

  const loadData = useCallback(async () => {
    try {
      const data = await api.getStrategies();
      setStrategies(data.strategies || []);
    } catch (e) {
      console.log('Strategy load error:', e.message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const onRefresh = () => { setRefreshing(true); loadData(); };

  const toggleStrategy = async (name, currentActive) => {
    setToggling(name);
    try {
      await api.activateStrategy(name, !currentActive);
      setStrategies(prev => prev.map(s =>
        s.name === name ? { ...s, active: !currentActive } : s
      ));
    } catch (e) {
      console.log('Toggle error:', e.message);
    } finally {
      setToggling(null);
    }
  };

  const grouped = {};
  strategies.forEach(s => {
    const cat = s.category || 'Other';
    grouped[cat] = grouped[cat] || [];
    grouped[cat].push(s);
  });

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color={COLORS.blue} />
      </View>
    );
  }

  const activeCount = strategies.filter(s => s.active).length;

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.blue} />}
    >
      {/* Summary */}
      <View style={styles.summaryRow}>
        <View style={styles.summaryBox}>
          <Text style={styles.summaryNum}>{activeCount}</Text>
          <Text style={styles.summaryLabel}>Active</Text>
        </View>
        <View style={styles.summaryBox}>
          <Text style={styles.summaryNum}>{strategies.length - activeCount}</Text>
          <Text style={styles.summaryLabel}>Inactive</Text>
        </View>
        <View style={styles.summaryBox}>
          <Text style={styles.summaryNum}>{Object.keys(grouped).length}</Text>
          <Text style={styles.summaryLabel}>Categories</Text>
        </View>
      </View>

      {/* Strategy Groups */}
      {Object.entries(grouped).map(([category, strats]) => (
        <View key={category} style={styles.group}>
          <View style={styles.groupHeader}>
            <Ionicons
              name={CATEGORY_ICONS[category] || 'ellipsis-horizontal-outline'}
              size={16} color={COLORS.blue}
            />
            <Text style={styles.groupTitle}>{category}</Text>
            <Text style={styles.groupCount}>
              {strats.filter(s => s.active).length}/{strats.length}
            </Text>
          </View>

          {strats.map(strat => (
            <Card key={strat.name} style={styles.stratCard}>
              <View style={styles.stratRow}>
                <View style={[styles.stratIndicator, { backgroundColor: STRATEGY_COLORS[strat.name] || COLORS.blue }]} />
                <View style={styles.stratInfo}>
                  <Text style={styles.stratName}>{strat.name}</Text>
                  <Text style={styles.stratDesc} numberOfLines={2}>{strat.description}</Text>
                  <View style={styles.stratMeta}>
                    <Text style={styles.weightLabel}>Weight: {strat.weight.toFixed(1)}x</Text>
                    <View style={styles.weightBar}>
                      <View style={[styles.weightFill, { width: `${Math.min(strat.weight / 1.5 * 100, 100)}%` }]} />
                    </View>
                  </View>
                </View>
                <View style={styles.toggleCol}>
                  {toggling === strat.name ? (
                    <ActivityIndicator size="small" color={COLORS.blue} />
                  ) : (
                    <Switch
                      value={strat.active}
                      onValueChange={() => toggleStrategy(strat.name, strat.active)}
                      trackColor={{ false: COLORS.bgInput, true: COLORS.blue + '66' }}
                      thumbColor={strat.active ? COLORS.blue : COLORS.textMuted}
                      ios_backgroundColor={COLORS.bgInput}
                    />
                  )}
                </View>
              </View>
            </Card>
          ))}
        </View>
      ))}

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 16, paddingTop: 8 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: COLORS.bg },

  summaryRow: { flexDirection: 'row', gap: 10, marginBottom: 16 },
  summaryBox: {
    flex: 1, backgroundColor: COLORS.bgCard, borderRadius: 10,
    borderWidth: 1, borderColor: COLORS.border, padding: 12, alignItems: 'center',
  },
  summaryNum: { fontSize: 24, fontWeight: '700', color: COLORS.blue },
  summaryLabel: { fontSize: 11, color: COLORS.textMuted, marginTop: 2, textTransform: 'uppercase' },

  group: { marginBottom: 8 },
  groupHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 8, paddingLeft: 4 },
  groupTitle: { color: COLORS.textPrimary, fontSize: 14, fontWeight: '600', flex: 1 },
  groupCount: { color: COLORS.textMuted, fontSize: 12 },

  stratCard: { padding: 12, marginBottom: 8 },
  stratRow: { flexDirection: 'row', alignItems: 'flex-start', gap: 10 },
  stratIndicator: { width: 4, borderRadius: 2, alignSelf: 'stretch', minHeight: 40 },
  stratInfo: { flex: 1 },
  stratName: { color: COLORS.textPrimary, fontSize: 15, fontWeight: '600' },
  stratDesc: { color: COLORS.textSecondary, fontSize: 12, marginTop: 3, lineHeight: 17 },
  stratMeta: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 6 },
  weightLabel: { color: COLORS.textMuted, fontSize: 10 },
  weightBar: { flex: 1, height: 3, backgroundColor: COLORS.bgInput, borderRadius: 2, maxWidth: 80 },
  weightFill: { height: 3, backgroundColor: COLORS.blue, borderRadius: 2 },
  toggleCol: { justifyContent: 'center', paddingTop: 4 },
});
