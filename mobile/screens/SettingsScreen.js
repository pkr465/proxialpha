import React, { useState, useEffect } from 'react';
import {
  View, Text, ScrollView, StyleSheet, TouchableOpacity,
  TextInput, Alert, Switch, Linking,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { COLORS } from '../theme';
import Card from '../components/Card';
import api from '../api';

export default function SettingsScreen() {
  const [serverUrl, setServerUrl] = useState(api.baseUrl);
  const [connected, setConnected] = useState(false);
  const [checking, setChecking] = useState(false);
  const [healthData, setHealthData] = useState(null);
  const [notifications, setNotifications] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [darkMode, setDarkMode] = useState(true);

  const checkConnection = async () => {
    setChecking(true);
    try {
      api.setBaseUrl(serverUrl);
      const data = await api.health();
      setConnected(true);
      setHealthData(data);
      Alert.alert('Connected', `Server is running\n${data.strategies_loaded} strategies loaded\n${data.tickers_loaded} tickers loaded`);
    } catch (e) {
      setConnected(false);
      setHealthData(null);
      Alert.alert('Connection Failed', `Could not reach server at ${serverUrl}\n\n${e.message}`);
    } finally {
      setChecking(false);
    }
  };

  useEffect(() => {
    api.health().then(data => { setConnected(true); setHealthData(data); }).catch(() => setConnected(false));
  }, []);

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      {/* Server Connection */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="server-outline" size={18} color={COLORS.blue} />
          <Text style={styles.cardTitle}>Server Connection</Text>
          <View style={[styles.statusDot, { backgroundColor: connected ? COLORS.green : COLORS.red }]} />
        </View>

        <Text style={styles.fieldLabel}>Server URL</Text>
        <TextInput
          style={styles.input}
          value={serverUrl}
          onChangeText={setServerUrl}
          placeholder="http://localhost:8000"
          placeholderTextColor={COLORS.textMuted}
          autoCapitalize="none"
          autoCorrect={false}
        />
        <Text style={styles.hint}>
          Android emulator: http://10.0.2.2:8000{'\n'}
          iOS simulator: http://localhost:8000{'\n'}
          Physical device: http://YOUR_IP:8000
        </Text>

        <TouchableOpacity style={styles.connectBtn} onPress={checkConnection} disabled={checking}>
          <Ionicons name={checking ? 'hourglass-outline' : 'link-outline'} size={16} color="#fff" />
          <Text style={styles.connectText}>{checking ? 'Checking...' : 'Test Connection'}</Text>
        </TouchableOpacity>

        {healthData && (
          <View style={styles.healthInfo}>
            <HealthRow label="Status" value={healthData.status} color={COLORS.green} />
            <HealthRow label="Strategies" value={String(healthData.strategies_loaded)} />
            <HealthRow label="Tickers" value={String(healthData.tickers_loaded)} />
            <HealthRow label="Last Check" value={new Date(healthData.timestamp).toLocaleTimeString()} />
          </View>
        )}
      </Card>

      {/* Preferences */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="options-outline" size={18} color={COLORS.purple} />
          <Text style={styles.cardTitle}>Preferences</Text>
        </View>

        <SettingRow
          icon="notifications-outline"
          label="Push Notifications"
          subtitle="Get alerted on new signals"
          value={notifications}
          onToggle={setNotifications}
        />
        <SettingRow
          icon="refresh-outline"
          label="Auto Refresh"
          subtitle="Refresh data every 60 seconds"
          value={autoRefresh}
          onToggle={setAutoRefresh}
        />
        <SettingRow
          icon="moon-outline"
          label="Dark Mode"
          subtitle="Always on for this release"
          value={darkMode}
          onToggle={setDarkMode}
          disabled
        />
      </Card>

      {/* About */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="information-circle-outline" size={18} color={COLORS.cyan} />
          <Text style={styles.cardTitle}>About</Text>
        </View>
        <View style={styles.aboutRow}>
          <Text style={styles.aboutLabel}>Version</Text>
          <Text style={styles.aboutValue}>2.0.0</Text>
        </View>
        <View style={styles.aboutRow}>
          <Text style={styles.aboutLabel}>Platform</Text>
          <Text style={styles.aboutValue}>React Native + Expo</Text>
        </View>
        <View style={styles.aboutRow}>
          <Text style={styles.aboutLabel}>Backend</Text>
          <Text style={styles.aboutValue}>FastAPI + WebSocket</Text>
        </View>
        <View style={styles.aboutRow}>
          <Text style={styles.aboutLabel}>Strategies</Text>
          <Text style={styles.aboutValue}>15 built-in</Text>
        </View>
        <View style={styles.aboutRow}>
          <Text style={styles.aboutLabel}>LLM Support</Text>
          <Text style={styles.aboutValue}>Claude, OpenAI, Ollama, Gemini</Text>
        </View>

        <View style={styles.divider} />

        <Text style={styles.appName}>ProxiAlpha</Text>
        <Text style={styles.appTagline}>proxiant.ai/proxialpha</Text>
      </Card>

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

function SettingRow({ icon, label, subtitle, value, onToggle, disabled }) {
  return (
    <View style={styles.settingRow}>
      <Ionicons name={icon} size={18} color={COLORS.textSecondary} />
      <View style={styles.settingInfo}>
        <Text style={styles.settingLabel}>{label}</Text>
        <Text style={styles.settingSub}>{subtitle}</Text>
      </View>
      <Switch
        value={value}
        onValueChange={onToggle}
        trackColor={{ false: COLORS.bgInput, true: COLORS.blue + '66' }}
        thumbColor={value ? COLORS.blue : COLORS.textMuted}
        disabled={disabled}
      />
    </View>
  );
}

function HealthRow({ label, value, color }) {
  return (
    <View style={styles.healthRow}>
      <Text style={styles.healthLabel}>{label}</Text>
      <Text style={[styles.healthValue, color && { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 16, paddingTop: 8 },

  cardHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 12 },
  cardTitle: { fontSize: 15, fontWeight: '600', color: COLORS.textPrimary, flex: 1 },
  statusDot: { width: 8, height: 8, borderRadius: 4 },

  fieldLabel: { fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 },
  input: {
    backgroundColor: COLORS.bgInput, borderRadius: 8, borderWidth: 1, borderColor: COLORS.border,
    paddingHorizontal: 12, paddingVertical: 10, color: COLORS.textPrimary, fontSize: 14,
  },
  hint: { color: COLORS.textMuted, fontSize: 11, lineHeight: 18, marginTop: 8, marginBottom: 12 },

  connectBtn: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 6,
    backgroundColor: COLORS.blue, borderRadius: 8, paddingVertical: 12,
  },
  connectText: { color: '#fff', fontSize: 14, fontWeight: '600' },

  healthInfo: { marginTop: 12, gap: 6, borderTopWidth: 1, borderTopColor: COLORS.border, paddingTop: 12 },
  healthRow: { flexDirection: 'row', justifyContent: 'space-between' },
  healthLabel: { color: COLORS.textMuted, fontSize: 12 },
  healthValue: { color: COLORS.textPrimary, fontSize: 12, fontWeight: '500' },

  settingRow: {
    flexDirection: 'row', alignItems: 'center', gap: 12,
    paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: COLORS.border,
  },
  settingInfo: { flex: 1 },
  settingLabel: { color: COLORS.textPrimary, fontSize: 14, fontWeight: '500' },
  settingSub: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },

  aboutRow: {
    flexDirection: 'row', justifyContent: 'space-between',
    paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: COLORS.border,
  },
  aboutLabel: { color: COLORS.textMuted, fontSize: 13 },
  aboutValue: { color: COLORS.textPrimary, fontSize: 13, fontWeight: '500' },

  divider: { height: 1, backgroundColor: COLORS.border, marginVertical: 12 },
  appName: { color: COLORS.textPrimary, fontSize: 20, fontWeight: '700', textAlign: 'center' },
  appTagline: { color: COLORS.textMuted, fontSize: 12, textAlign: 'center', marginTop: 4 },
});
