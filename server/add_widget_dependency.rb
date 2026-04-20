require 'xcodeproj'

project_path = '/Users/pavel/Desktop/IOS/Unity-iPhone.xcodeproj'
project = Xcodeproj::Project.open(project_path)

widget_target_name = 'URLImageWidget'
app_target = project.targets.find { |t| t.name == 'Unity-iPhone' }
abort("❌ Не найден основной таргет") unless app_target

# Создаём новый таргет для виджета
widget_target = project.new_target(:app_extension, widget_target_name, :ios, '17.0', nil, :swift)

# Указываем Bundle Identifier и Info.plist путь
plist_path = 'Widgets/Info.plist'
widget_target.build_configurations.each do |config|
  config.build_settings['PRODUCT_BUNDLE_IDENTIFIER'] = 'com.PavelKhabusov.KartotekaAR.widget'
  config.build_settings['INFOPLIST_FILE'] = plist_path
  config.build_settings['CODE_SIGN_ENTITLEMENTS'] = 'Unity-iPhone.entitlements'
  config.build_settings['DEVELOPMENT_TEAM'] = '7W3GJTY422'
  config.build_settings['SWIFT_VERSION'] = '5.0'
  config.build_settings['EMBEDDED_CONTENT_CONTAINS_SWIFT'] = 'YES'
  config.build_settings['LD_RUNPATH_SEARCH_PATHS'] = '$(inherited) @executable_path/Frameworks'
  config.build_settings['PRODUCT_NAME'] = 'URLImageWidget'
  config.build_settings['WRAPPER_EXTENSION'] = 'appex'
  config.build_settings['ENABLE_APPINTENTS_SUGGESTIONS_TRAINING'] = 'NO'
  config.build_settings['ENABLE_APP_SHORTCUTS_FLEXIBLE_MATCHING'] = 'NO'
  # config.build_settings['SWIFT_OPTIMIZATION_LEVEL'] = '-Onone'
end

# Добавляем Swift-файлы в новую группу и таргет
group = project.main_group.find_subpath('Widgets/', true)
group.set_source_tree('SOURCE_ROOT')
%w[
  URLImageWidget/ProductData.swift
  URLImageWidget/URLImageWidget.swift
  URLImageWidget/URLImageWidget+Provider.swift
  WidgetBundle.swift
].each do |filename|
  file_path = "Widgets/#{filename}"
  file_ref = group.new_file(file_path)
  build_phase = widget_target.source_build_phase
  build_phase.add_file_reference(file_ref, true)
end

frameworks_phase = widget_target.frameworks_build_phase
#widget_target.add_system_framework('Foundation')

frameworks_phase.files.each do |build_file|
    file_ref = build_file.file_ref
    if file_ref && file_ref.path.to_s.downcase.include?('foundation.framework')
      build_file.remove_from_project
      file_ref.remove_from_project
    end
end

%w[
    Foundation
    SwiftUI
    WidgetKit
    Combine
    AppIntents
].each do |framework_name|
  framework_path = "/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk/System/Library/Frameworks/#{framework_name}.framework"
  
  # Проверим, не добавлен ли уже такой фреймворк (по имени, не по пути)
  existing_file = project.frameworks_group.files.find do |f|
    File.basename(f.path.to_s) == framework_name
  end

  unless existing_file
    file_ref = project.frameworks_group.new_file(framework_path)
    frameworks_phase.add_file_reference(file_ref, true)
  end
end

copy_phase = app_target.copy_files_build_phases.find { |bp| bp.name == 'Embed App Extensions' }
unless copy_phase
  copy_phase = app_target.new_copy_files_build_phase('Embed App Extensions')
  copy_phase.dst_subfolder_spec = '13'
end
copy_phase.add_file_reference(widget_target.product_reference, true)

# Сохраняем проект
project.save
puts "✅ Виджет #{widget_target_name} успешно добавлен в проект"
