import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, ScrollView, StyleSheet, TouchableOpacity,
  ActivityIndicator, TextInput, KeyboardAvoidingView, Platform,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { COLORS } from '../theme';
import Card from '../components/Card';
import api from '../api';

const PROVIDERS = [
  { id: 'anthropic', name: 'Claude', icon: 'sparkles-outline', color: '#ec4899', defaultModel: 'claude-sonnet-4-20250514' },
  { id: 'openai', name: 'OpenAI', icon: 'cube-outline', color: '#10b981', defaultModel: 'gpt-4o' },
  { id: 'ollama', name: 'Ollama', icon: 'server-outline', color: '#f59e0b', defaultModel: 'llama3' },
  { id: 'gemini', name: 'Gemini', icon: 'diamond-outline', color: '#3b82f6', defaultModel: 'gemini-pro' },
  { id: 'openai_compatible', name: 'Custom', icon: 'code-outline', color: '#8b5cf6', defaultModel: 'custom-model' },
];

const QUICK_PROMPTS = [
  { label: 'Market Analysis', prompt: 'Analyze the current market conditions and identify key opportunities', icon: 'analytics-outline' },
  { label: 'Risk Assessment', prompt: 'Evaluate portfolio risk exposure and suggest risk mitigation strategies', icon: 'shield-outline' },
  { label: 'Strategy Review', prompt: 'Review active trading strategies and suggest optimizations', icon: 'options-outline' },
  { label: 'Sector Outlook', prompt: 'Provide a sector-by-sector outlook for the next quarter', icon: 'grid-outline' },
];

export default function AILabScreen() {
  const [selectedProvider, setSelectedProvider] = useState(PROVIDERS[0]);
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(PROVIDERS[0].defaultModel);
  const [baseUrl, setBaseUrl] = useState('');
  const [configured, setConfigured] = useState(false);
  const [configuring, setConfiguring] = useState(false);

  const [prompt, setPrompt] = useState('');
  const [ticker, setTicker] = useState('');
  const [includePortfolio, setIncludePortfolio] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [response, setResponse] = useState(null);
  const [chatHistory, setChatHistory] = useState([]);

  const configureLLM = async () => {
    setConfiguring(true);
    try {
      await api.configureLLM(
        selectedProvider.id,
        model,
        apiKey || undefined,
        baseUrl || undefined,
      );
      setConfigured(true);
    } catch (e) {
      console.log('Configure error:', e.message);
    } finally {
      setConfiguring(false);
    }
  };

  const analyze = async (promptText) => {
    const p = promptText || prompt;
    if (!p.trim()) return;

    setAnalyzing(true);
    setChatHistory(prev => [...prev, { role: 'user', content: p }]);
    setPrompt('');

    try {
      const res = await api.analyzeLLM(ticker || undefined, p, includePortfolio);
      setResponse(res);
      setChatHistory(prev => [...prev, {
        role: 'assistant',
        content: res.response,
        model: res.model,
        provider: res.provider,
      }]);
    } catch (e) {
      setChatHistory(prev => [...prev, {
        role: 'error',
        content: e.message,
      }]);
    } finally {
      setAnalyzing(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      keyboardVerticalOffset={90}
    >
      <ScrollView style={styles.scroll} contentContainerStyle={styles.content}>
        {/* Provider Selector */}
        <Card>
          <View style={styles.cardHeader}>
            <Ionicons name="hardware-chip-outline" size={18} color={COLORS.purple} />
            <Text style={styles.cardTitle}>LLM Provider</Text>
            {configured && (
              <View style={styles.configuredBadge}>
                <Ionicons name="checkmark-circle" size={14} color={COLORS.green} />
                <Text style={styles.configuredText}>Connected</Text>
              </View>
            )}
          </View>

          <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.providerRow}>
            {PROVIDERS.map(p => (
              <TouchableOpacity
                key={p.id}
                style={[styles.providerChip, selectedProvider.id === p.id && { borderColor: p.color, backgroundColor: p.color + '18' }]}
                onPress={() => {
                  setSelectedProvider(p);
                  setModel(p.defaultModel);
                  setConfigured(false);
                }}
              >
                <Ionicons name={p.icon} size={16} color={selectedProvider.id === p.id ? p.color : COLORS.textMuted} />
                <Text style={[styles.providerName, selectedProvider.id === p.id && { color: p.color }]}>{p.name}</Text>
              </TouchableOpacity>
            ))}
          </ScrollView>

          {/* Config Fields */}
          <View style={styles.configFields}>
            <Text style={styles.fieldLabel}>Model</Text>
            <TextInput
              style={styles.input}
              value={model}
              onChangeText={setModel}
              placeholder="Model name"
              placeholderTextColor={COLORS.textMuted}
            />

            {selectedProvider.id !== 'ollama' && (
              <>
                <Text style={styles.fieldLabel}>API Key</Text>
                <TextInput
                  style={styles.input}
                  value={apiKey}
                  onChangeText={setApiKey}
                  placeholder="Enter API key"
                  placeholderTextColor={COLORS.textMuted}
                  secureTextEntry
                />
              </>
            )}

            {(selectedProvider.id === 'ollama' || selectedProvider.id === 'openai_compatible') && (
              <>
                <Text style={styles.fieldLabel}>Base URL</Text>
                <TextInput
                  style={styles.input}
                  value={baseUrl}
                  onChangeText={setBaseUrl}
                  placeholder={selectedProvider.id === 'ollama' ? 'http://localhost:11434' : 'http://your-server:8080/v1'}
                  placeholderTextColor={COLORS.textMuted}
                />
              </>
            )}

            <TouchableOpacity style={[styles.configBtn, { backgroundColor: selectedProvider.color }]} onPress={configureLLM} disabled={configuring}>
              {configuring ? (
                <ActivityIndicator size="small" color="#fff" />
              ) : (
                <Text style={styles.configBtnText}>Configure {selectedProvider.name}</Text>
              )}
            </TouchableOpacity>
          </View>
        </Card>

        {/* Quick Prompts */}
        <Card>
          <View style={styles.cardHeader}>
            <Ionicons name="flash-outline" size={18} color={COLORS.amber} />
            <Text style={styles.cardTitle}>Quick Analysis</Text>
          </View>
          <View style={styles.quickGrid}>
            {QUICK_PROMPTS.map(qp => (
              <TouchableOpacity
                key={qp.label}
                style={styles.quickBtn}
                onPress={() => analyze(qp.prompt)}
                disabled={!configured || analyzing}
              >
                <Ionicons name={qp.icon} size={18} color={COLORS.blue} />
                <Text style={styles.quickLabel}>{qp.label}</Text>
              </TouchableOpacity>
            ))}
          </View>
        </Card>

        {/* Chat History */}
        {chatHistory.length > 0 && (
          <Card>
            <View style={styles.cardHeader}>
              <Ionicons name="chatbubbles-outline" size={18} color={COLORS.cyan} />
              <Text style={styles.cardTitle}>Conversation</Text>
              <TouchableOpacity onPress={() => setChatHistory([])}>
                <Ionicons name="trash-outline" size={16} color={COLORS.textMuted} />
              </TouchableOpacity>
            </View>
            {chatHistory.map((msg, i) => (
              <View key={i} style={[styles.chatBubble, msg.role === 'user' ? styles.userBubble : msg.role === 'error' ? styles.errorBubble : styles.aiBubble]}>
                {msg.role === 'assistant' && (
                  <Text style={styles.chatMeta}>{msg.provider} / {msg.model}</Text>
                )}
                <Text style={[styles.chatText, msg.role === 'error' && { color: COLORS.red }]}>
                  {msg.content}
                </Text>
              </View>
            ))}
            {analyzing && (
              <View style={styles.analyzingRow}>
                <ActivityIndicator size="small" color={selectedProvider.color} />
                <Text style={styles.analyzingText}>Analyzing...</Text>
              </View>
            )}
          </Card>
        )}

        <View style={{ height: 100 }} />
      </ScrollView>

      {/* Input Bar */}
      <View style={styles.inputBar}>
        <View style={styles.inputRow}>
          <TextInput
            style={styles.chatInput}
            value={prompt}
            onChangeText={setPrompt}
            placeholder={configured ? 'Ask the AI anything...' : 'Configure a provider first'}
            placeholderTextColor={COLORS.textMuted}
            multiline
            editable={configured}
          />
          <TouchableOpacity
            style={[styles.sendBtn, { backgroundColor: selectedProvider.color }]}
            onPress={() => analyze()}
            disabled={!configured || analyzing || !prompt.trim()}
          >
            {analyzing ? (
              <ActivityIndicator size="small" color="#fff" />
            ) : (
              <Ionicons name="send" size={18} color="#fff" />
            )}
          </TouchableOpacity>
        </View>

        {/* Context toggles */}
        <View style={styles.contextRow}>
          <TouchableOpacity
            style={[styles.contextChip, ticker && styles.contextChipActive]}
            onPress={() => setTicker(ticker ? '' : 'AAPL')}
          >
            <Ionicons name="pricetag-outline" size={12} color={ticker ? COLORS.blue : COLORS.textMuted} />
            <Text style={[styles.contextText, ticker && { color: COLORS.blue }]}>
              {ticker || 'Add ticker'}
            </Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.contextChip, includePortfolio && styles.contextChipActive]}
            onPress={() => setIncludePortfolio(!includePortfolio)}
          >
            <Ionicons name="briefcase-outline" size={12} color={includePortfolio ? COLORS.green : COLORS.textMuted} />
            <Text style={[styles.contextText, includePortfolio && { color: COLORS.green }]}>Portfolio</Text>
          </TouchableOpacity>
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  scroll: { flex: 1 },
  content: { padding: 16, paddingTop: 8 },

  cardHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 12 },
  cardTitle: { fontSize: 15, fontWeight: '600', color: COLORS.textPrimary, flex: 1 },

  configuredBadge: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  configuredText: { color: COLORS.green, fontSize: 11, fontWeight: '600' },

  providerRow: { marginBottom: 12 },
  providerChip: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 14, paddingVertical: 10, borderRadius: 10,
    backgroundColor: COLORS.bgInput, borderWidth: 1, borderColor: COLORS.border,
    marginRight: 8,
  },
  providerName: { color: COLORS.textSecondary, fontSize: 13, fontWeight: '600' },

  configFields: { gap: 8 },
  fieldLabel: { fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.5 },
  input: {
    backgroundColor: COLORS.bgInput, borderRadius: 8, borderWidth: 1, borderColor: COLORS.border,
    paddingHorizontal: 12, paddingVertical: 10, color: COLORS.textPrimary, fontSize: 14,
  },
  configBtn: { paddingVertical: 12, borderRadius: 8, alignItems: 'center', marginTop: 4 },
  configBtnText: { color: '#fff', fontSize: 14, fontWeight: '600' },

  quickGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  quickBtn: {
    width: '47%', flexDirection: 'row', alignItems: 'center', gap: 8,
    backgroundColor: COLORS.bgInput, borderRadius: 10, borderWidth: 1, borderColor: COLORS.border,
    paddingHorizontal: 12, paddingVertical: 12,
  },
  quickLabel: { color: COLORS.textSecondary, fontSize: 12, fontWeight: '500' },

  chatBubble: { padding: 12, borderRadius: 12, marginBottom: 8 },
  userBubble: { backgroundColor: COLORS.blue + '22', marginLeft: 40, borderBottomRightRadius: 4 },
  aiBubble: { backgroundColor: COLORS.bgInput, marginRight: 20, borderBottomLeftRadius: 4 },
  errorBubble: { backgroundColor: COLORS.red + '15', marginRight: 20, borderBottomLeftRadius: 4 },
  chatMeta: { fontSize: 10, color: COLORS.textMuted, marginBottom: 4 },
  chatText: { color: COLORS.textPrimary, fontSize: 13, lineHeight: 20 },

  analyzingRow: { flexDirection: 'row', alignItems: 'center', gap: 8, padding: 8 },
  analyzingText: { color: COLORS.textMuted, fontSize: 12 },

  inputBar: {
    backgroundColor: COLORS.bgCard, borderTopWidth: 1, borderTopColor: COLORS.border,
    paddingHorizontal: 12, paddingVertical: 8, paddingBottom: 20,
  },
  inputRow: { flexDirection: 'row', alignItems: 'flex-end', gap: 8 },
  chatInput: {
    flex: 1, backgroundColor: COLORS.bgInput, borderRadius: 20, borderWidth: 1,
    borderColor: COLORS.border, paddingHorizontal: 16, paddingVertical: 10,
    color: COLORS.textPrimary, fontSize: 14, maxHeight: 100,
  },
  sendBtn: { width: 40, height: 40, borderRadius: 20, alignItems: 'center', justifyContent: 'center' },

  contextRow: { flexDirection: 'row', gap: 8, marginTop: 6, paddingLeft: 4 },
  contextChip: {
    flexDirection: 'row', alignItems: 'center', gap: 4,
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 12,
    backgroundColor: COLORS.bgInput, borderWidth: 1, borderColor: COLORS.border,
  },
  contextChipActive: { borderColor: COLORS.blue + '55' },
  contextText: { color: COLORS.textMuted, fontSize: 11 },
});
