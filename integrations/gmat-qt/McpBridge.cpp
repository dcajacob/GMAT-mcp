#include "McpBridge.hpp"
#include "MainWindow.hpp"
#include "ScriptDocument.hpp"
#include "EditablePanel.hpp"
#include "QtSolverListener.hpp"
#include "Moderator.hpp"
#include "FileManager.hpp"
#include "GmatGlobal.hpp"
#include "GmatBase.hpp"
#include "Parameter.hpp"
#include "GmatCommand.hpp"
#include "SolverBranchCommand.hpp"
#include "SolarSystem.hpp"
#include "CelestialBody.hpp"
#include "SpecialCelestialPoint.hpp"
#include "HelpSystem.hpp"
#include <QThread>
#include <QTcpSocket>
#include <QJsonDocument>
#include <QJsonArray>
#include <QDir>
#include <QFile>
#include <QSaveFile>
#include <QCryptographicHash>
#include <QRandomGenerator>
#include <QUuid>
#include <QCoreApplication>
#include <QPlainTextEdit>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QTimer>
#include <QDateTime>
#include <QSysInfo>
#include <QFileInfo>
#include <cmath>
#include <stdexcept>
#ifdef Q_OS_WIN
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#include <vector>
#endif

namespace {
QString stamp() { return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs); }
QString installationRoot() {
   const QDir binary(QCoreApplication::applicationDirPath());
   if (binary.dirName()=="MacOS" && QDir(binary.absoluteFilePath("..")).dirName()=="Contents") {
      const QDir app(binary.absoluteFilePath("../.."));
      const QDir parent(app.absoluteFilePath(".."));
      if(parent.dirName()=="bin" && QFileInfo::exists(parent.filePath("gmat_startup_file.txt")))
         return QDir(parent.absoluteFilePath("..")).absolutePath();
      return app.absolutePath();
   }
   return QDir(binary.absoluteFilePath("..")).absolutePath();
}
void protect(const QString &path, bool directory) {
#ifdef Q_OS_WIN
   HANDLE token=nullptr;
   if (!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)) throw std::runtime_error("Cannot query bridge user");
   DWORD size=0;
   GetTokenInformation(token,TokenUser,nullptr,0,&size);
   std::vector<unsigned char> user(size);
   const auto queried=GetTokenInformation(token,TokenUser,user.data(),size,&size);
   CloseHandle(token);
   if (!queried) throw std::runtime_error("Cannot read bridge user");
   auto *sid=reinterpret_cast<TOKEN_USER*>(user.data())->User.Sid;
   LPWSTR sidText=nullptr;
   if (!ConvertSidToStringSidW(sid,&sidText)) throw std::runtime_error("Cannot encode bridge user");
   const auto sddl=QString("D:P(A;%1;FA;;;%2)").arg(directory?"OICI":"",QString::fromWCharArray(sidText));
   LocalFree(sidText);
   PSECURITY_DESCRIPTOR descriptor=nullptr;
   if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(reinterpret_cast<LPCWSTR>(sddl.utf16()),SDDL_REVISION_1,&descriptor,nullptr))
      throw std::runtime_error("Cannot create private bridge ACL");
   BOOL present=false,defaulted=false;PACL acl=nullptr;
   GetSecurityDescriptorDacl(descriptor,&present,&acl,&defaulted);
   const auto result=SetNamedSecurityInfoW(const_cast<LPWSTR>(reinterpret_cast<LPCWSTR>(path.utf16())),SE_FILE_OBJECT,
      OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION|PROTECTED_DACL_SECURITY_INFORMATION,sid,nullptr,acl,nullptr);
   LocalFree(descriptor);
   if (result!=ERROR_SUCCESS) throw std::runtime_error("Cannot protect private bridge discovery record");
#else
   if (!QFile::setPermissions(path,QFile::ReadOwner|QFile::WriteOwner|(directory?QFile::ExeOwner:QFile::Permissions{})))
      throw std::runtime_error("Cannot protect private bridge discovery record");
#endif
}
void save(const QString &path,const QByteArray &data) {
   QSaveFile file(path);
   if (!file.open(QIODevice::WriteOnly) || file.write(data)!=data.size() || !file.commit())
      throw std::runtime_error("Cannot save bridge artifact");
}
QJsonObject field(GmatBase *object,const std::string &name) {
   const auto id=object->GetParameterID(name);
   const auto type=object->GetParameterTypeString(id);
   const auto text=QString::fromStdString(object->GetField(id));
   QJsonValue value=text;
   bool typed=false;
   if (type=="Real") { const auto n=object->GetRealParameter(id);value=std::isfinite(n)?QJsonValue(n):QJsonValue();typed=true; }
   else if (type=="Integer") { value=object->GetIntegerParameter(id);typed=true; }
   else if (type=="Boolean") { value=object->GetBooleanParameter(id);typed=true; }
   QJsonValue unit,readonly;
   try { unit=QString::fromStdString(object->GetParameterUnit(id)); } catch (...) {}
   try { readonly=object->IsParameterReadOnly(id); } catch (...) {}
   return {{"name",QString::fromStdString(name)},{"type",QString::fromStdString(type)},{"text",text},{"value",value},{"typed",typed},{"unit",unit},{"read_only",readonly}};
}
}
McpBridge::McpBridge(MainWindow &value,const QString &directory) : QObject(&value),window(value),root(QDir(directory).absolutePath())
{
   QDir().mkpath(root);
   protect(root,true);
   desktopId=QUuid::createUuid().toString(QUuid::Id128);
   for (int i=0;i<8;++i) token+=QString::number(QRandomGenerator::system()->generate(),16).rightJustified(8,'0');
   if (!server.listen(QHostAddress::LocalHost,0)) throw std::runtime_error("Cannot listen on loopback for MCP");
   recordPath=root+"/"+desktopId+".json";
   save(recordPath,QJsonDocument(QJsonObject{{"protocol",1},{"desktop_id",desktopId},{"pid",QCoreApplication::applicationPid()},
      {"port",server.serverPort()},{"token",token},{"installation_root",installationRoot()}}).toJson());
   protect(recordPath,false);
   window.mcpBridge=this;
   QJsonArray classes;for(const auto &name:Moderator::Instance()->GetListOfAllFactoryItems()) classes.append(QString::fromStdString(name));
   engineInfo={{"python","desktop C++"},{"platform",QSysInfo::prettyProductName()},{"api","GMAT-Qt live desktop bridge"},
      {"version",QString::fromStdString(GmatGlobal::Instance()->GetGmatVersion())},{"build_date",QString::fromStdString(GmatGlobal::Instance()->GetGMATBuildDate())},
      {"native_libraries",QJsonArray{}},{"classes",classes},{"capabilities",QJsonArray{"objects","commands","scripts","execution","runtime_state","live_desktop","pause_resume","solver_listener"}}};
   connect(&server,&QTcpServer::newConnection,this,[this] {
      while (auto *socket=server.nextPendingConnection()) {
         socket->setReadBufferSize(1024*1024+1);
         connect(socket,&QTcpSocket::readyRead,this,[this,socket] {
            if (socket->bytesAvailable()>1024*1024) { socket->abort();return; }
            while (socket->canReadLine()) {
               QJsonObject reply;
               try {
                  const auto bytes=socket->readLine(1024*1024+1);
                  QJsonParseError error;const auto parsed=QJsonDocument::fromJson(bytes,&error);
                  if (error.error!=QJsonParseError::NoError || !parsed.isObject()) throw std::runtime_error("INVALID_REQUEST");
                  const auto request=parsed.object();
                  // Authenticate every request; never echo the request or token.
                  const auto candidate=request.value("token").toString().toLatin1();
                  const auto expected=token.toLatin1(); unsigned diff=candidate.size()^expected.size();
                  for (int i=0;i<expected.size();++i) diff|=unsigned(expected[i]^(i<candidate.size()?candidate[i]:0));
                  if (diff) throw std::runtime_error("AUTHENTICATION_FAILED");
                  reply={{"ok",true},{"result",dispatch(request,socket)}};
               } catch (BaseException &e) { reply={{"ok",false},{"error",QJsonObject{{"code","DESKTOP_ERROR"},{"message",QString::fromStdString(e.GetFullMessage())}}}}; }
               catch (std::exception &e) { reply={{"ok",false},{"error",QJsonObject{{"code",QString::fromUtf8(e.what())},{"message",QString::fromUtf8(e.what())}}}}; }
               const auto body=QJsonDocument(reply).toJson(QJsonDocument::Compact)+'\n';
               if (body.size()>1024*1024) { socket->write("{\"ok\":false,\"error\":{\"code\":\"RESPONSE_LIMIT\",\"message\":\"Response too large\"}}\n"); }
               else socket->write(body);
               socket->flush();
            }
         });
         connect(socket,&QTcpSocket::disconnected,this,[this,socket] {
            if (controller==socket) {
               controller=nullptr;
               if (window.running && run.value("owner")=="mcp") window.stopMission();
            }
            socket->deleteLater();
         });
      }
   });
}
McpBridge::~McpBridge() {
   window.mcpBridge=nullptr;controller=nullptr;server.close();
   for(auto *socket:server.findChildren<QTcpSocket *>()) {QObject::disconnect(socket,nullptr,this,nullptr);socket->abort();}
   QFile::remove(recordPath);
}
QJsonObject McpBridge::state()
{
   if (document!=window.activeDocument.get()) { document=window.activeDocument.get();documentId=QUuid::createUuid().toString(QUuid::Id128);projectRoot={}; }
   const auto source=window.editor->toPlainText();
   const auto version=QCryptographicHash::hash((source+window.builtScript+QString::number(window.modelValid)+QString::number(window.editor->document()->revision())+documentId+window.scriptPath+projectRoot).toUtf8(),QCryptographicHash::Sha256).toHex();
   return {{"desktop_id",desktopId},{"document_id",documentId},{"model_version",QString(version)},{"running",window.running},{"paused",window.paused},
      {"dirty",source!=window.builtScript},{"built",window.modelValid},{"path",window.scriptPath},{"project_dir",projectDirectory().isEmpty()?QFileInfo(window.scriptPath).absolutePath():projectDirectory()},{"startup",window.startupFile},{"engine",engineInfo},{"run",run}};
}
QString McpBridge::projectDirectory() const { return document==window.activeDocument.get() ? projectRoot : QString(); }
void McpBridge::guard(const QJsonObject &args,bool version)
{
   const auto current=state();
   if (args.value("document_id")!=current.value("document_id")) throw std::runtime_error("DOCUMENT_CHANGED");
   if (window.running) throw std::runtime_error("SESSION_BUSY");
   if (version && args.value("expected_version")!=current.value("model_version")) throw std::runtime_error("STALE_VERSION");
}
QJsonObject McpBridge::dispatch(const QJsonObject &request,QTcpSocket *socket)
{
   if(QThread::currentThread()!=window.thread()) throw std::runtime_error("THREAD_OWNERSHIP_ERROR");
   const auto op=request.value("operation").toString();const auto a=request.value("arguments").toObject();
   if (op=="hello") return state();
   if (op=="attach") { if (controller && controller!=socket) throw std::runtime_error("DESKTOP_ATTACHED");controller=socket;return state(); }
   if (controller!=socket) throw std::runtime_error("NOT_ATTACHED");
   if (op=="state") return state();
   if (op=="stop" || op=="pause" || op=="resume") {
      if (a.value("document_id")!=state().value("document_id")) throw std::runtime_error("DOCUMENT_CHANGED");
      if (op=="stop") window.stopMission();else if(op=="pause") window.pauseMission();else window.resumeMission();return state();
   }
   if (op=="logs") {
      const auto bytes=window.messages->toPlainText().toUtf8();const auto offset=a.value("offset").toInt();const auto limit=a.value("limit").toInt(16384);
      if(offset<0 || limit<1 || limit>65536) throw std::runtime_error("INVALID_RANGE");
      const auto chunk=bytes.mid(offset,limit);return {{"content",QString::fromLatin1(chunk.toBase64())},{"encoding","base64"},{"offset",offset},{"next_offset",offset+chunk.size()},{"has_more",offset+chunk.size()<bytes.size()}};
   }
   guard(a,op=="apply_script" || op=="build" || op=="start_run");
   if (op=="export_script") return {{"text",window.builtScript},{"project_dir",QFileInfo(window.scriptPath).absolutePath()},{"model_version",state()["model_version"]},{"dirty",window.editor->toPlainText()!=window.builtScript}};
   if (op=="build" || op=="apply_script") {
      for (auto *child:window.workspace->subWindowList()) if (auto *panel=dynamic_cast<EditablePanel *>(child->widget());panel && panel->hasUnappliedInput()) throw std::runtime_error("PENDING_PANEL_CHANGES");
      if (op=="build") { if(!window.buildScript()) throw std::runtime_error("BUILD_FAILED"); }
      else {
         if (window.editor->toPlainText()!=window.builtScript) throw std::runtime_error("UNBUILT_EDITOR");
         const auto previous=projectRoot;
         if(a.contains("project_dir")) {
            const auto path=a.value("project_dir").toString();if(!QDir(path).exists()) throw std::runtime_error("MISSING_PROJECT_DIRECTORY");projectRoot=path;
         }
         const auto error=window.applyModelScript(a.value("text").toString());
         if (!error.isEmpty()) { projectRoot=previous;throw std::runtime_error(error.toStdString()); }
      }
      return state();
   }
   if (op=="start_run") {
      if (window.editor->toPlainText()!=window.builtScript || !window.modelValid) throw std::runtime_error("UNBUILT_EDITOR");
      pending=a;
      QTimer::singleShot(0,this,[this] {
         const auto result=window.runMission();
         if (!pending.isEmpty()) { run=pending;run["status"]="failed";run["native_status"]=-9;pending={}; }
         (void)result;
      });
      return {{"accepted",true}};
   }
   auto *moderator=Moderator::Instance();
   if(op=="get_help") {
      const auto topic=a.value("topic").toString().toStdString();auto *object=moderator->GetConfiguredObject(topic);
      const auto help=object?object->Help():HelpSystem::Instance()->Help(topic);
      return {{"text",QString::fromStdString(help).left(65536)}};
   }
   if (op=="list_objects") { QJsonArray names;for(const auto &n:moderator->GetListOfObjects(Gmat::UNKNOWN_OBJECT)) names.append(QString::fromStdString(n));return {{"objects",names}}; }
   if (op=="list_classes") return {{"classes",engineInfo["classes"]}};
   if (op=="list_commands") {
      std::function<QJsonArray(const QVector<int>&)> nodes=[&](const QVector<int> &indices) {
         QJsonArray out;for(const auto i:indices) {const auto &n=window.missionState.nodes[i];out.append(QJsonObject{{"id",QString::number(i)},{"type",n.type},{"text",n.statement},{"children",nodes(n.children)}});}return out;
      };return {{"commands",nodes(window.missionState.roots)}};
   }
   if (op=="get_object") {
      const auto name=a.value("name").toString().toStdString();
      auto *object=a.value("view")=="runtime" ? moderator->GetInternalObject(name) : moderator->GetConfiguredObject(name);
      if(!object && a.value("view")!="runtime") {auto *system=moderator->GetSolarSystemInUse();object=system->GetBody(name);if(!object) object=system->GetSpecialPoint(name);}
      if (!object) throw std::runtime_error("UNKNOWN_OBJECT");
      QJsonArray fields;const auto selected=a.value("fields").toArray();
      if (selected.isEmpty()) for(int i=0;i<object->GetParameterCount();++i) { try {fields.append(field(object,object->GetParameterText(i)));} catch (...) {} }
      else for(const auto &n:selected) fields.append(field(object,n.toString().toStdString()));
      QJsonObject context;
      if(object->IsOfType("Spacecraft")) for(const auto *key:{"Epoch","DateFormat","CoordinateSystem","DisplayStateType"}) try{context[key]=QString::fromStdString(object->GetField(key));}catch(...){}
      return {{"name",QString::fromStdString(name)},{"type",QString::fromStdString(object->GetTypeName())},{"fields",fields},{"context",context},{"view",a.value("view").toString("configured")}};
   }
   if (op=="parameter_metadata") { QJsonArray out;for(const auto &v:a.value("expressions").toArray()) {auto *p=moderator->GetConfiguredObject(v.toString().toStdString());if(!p) throw std::runtime_error("UNKNOWN_PARAMETER");out.append(QJsonObject{{"unit",QString::fromStdString(p->GetStringParameter("Unit"))},{"unit_source","native metadata"}});}return {{"parameters",out}}; }
   throw std::runtime_error("UNSUPPORTED_CAPABILITY");
}
void McpBridge::missionStarted()
{
   run=pending;pending={};
   if(run.isEmpty()) run={{"run_id",QUuid::createUuid().toString(QUuid::Id128)},{"owner","desktop"},{"directory",root+"/runs/"+QUuid::createUuid().toString(QUuid::Id128)}};
   run["status"]="running";run["started_at"]=stamp();run["model_version"]=state()["model_version"];
   const auto directory=run.value("directory").toString();QDir().mkpath(directory+"/output");
   save(directory+"/mission.script",window.builtScript.toUtf8());
   QFile::copy(window.startupFile,directory+"/effective_startup.txt");
   auto *manager=FileManager::Instance();previousOutput=QString::fromStdString(manager->GetFullPathname("OUTPUT_PATH"));
   manager->SetAbsPathname("OUTPUT_PATH",(directory+"/output/").toStdString());
   for(const auto &name:Moderator::Instance()->GetListOfObjects(Gmat::SUBSCRIBER)) {
      auto *o=Moderator::Instance()->GetConfiguredObject(name);
      if(o && (o->IsOfType("ReportFile") || o->IsOfType("EphemerisFile"))) o->SetStringParameter("Filename",o->GetStringParameter("Filename"));
   }
}
void McpBridge::missionFinished(int nativeStatus)
{
   if(run.isEmpty()) return;
   run["native_status"]=nativeStatus;run["status"]=nativeStatus==1?"succeeded":nativeStatus==-4?"cancelled":"failed";run["finished_at"]=stamp();
   const auto directory=run.value("directory").toString();
   save(directory+"/engine.log",window.messages->toPlainText().toUtf8());
   const auto listeners=window.solverListeners->snapshot();QJsonArray solvers;
   for(auto *command:window.runCommands) if(command && (command->GetTypeName()=="Target" || command->GetTypeName()=="Optimize")) {
      auto *clone=command->GetClone(0);if(!clone) continue;
      const auto name=QString::fromStdString(clone->GetName());QJsonObject record;
      for(const auto &entry:listeners) if(entry.toObject().value("name").toString().contains(name)) {record=entry.toObject();break;}
      const auto status=clone->GetIntegerParameter("IntegerSolverStatus");
      record["name"]=name;record["type"]=QString::fromStdString(clone->GetTypeName());record["native_status"]=status;
      record["convergence"]=status==1 || status==5 ? "converged" : status<0 && status!=-10 ? "not_converged" : status==0 || status==2 || status==3 ? "not_run" : "unavailable";
      record["report"]=QString::fromStdString(clone->GetStringParameter("ReportFile"));record["source"]="Qt native solver clone and listener";
      solvers.append(record);
   }
   save(directory+"/solvers.json",QJsonDocument(solvers).toJson());
   save(directory+"/desktop-run.json",QJsonDocument(run).toJson());
   if(!previousOutput.isEmpty()) FileManager::Instance()->SetAbsPathname("OUTPUT_PATH",previousOutput.toStdString());
}
