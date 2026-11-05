from testnew import analyze_audio_with_gemini, convert_audio

converted = convert_audio('losssale.wav')
result = analyze_audio_with_gemini(converted)

print('===FINAL TRANSCRIPT===')
print(result[0])
print('===FINAL SUMMARY===')
print(result[4])